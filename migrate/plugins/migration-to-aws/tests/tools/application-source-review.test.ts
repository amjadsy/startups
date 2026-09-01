// Focused tests for the read-only source_review phase: they import the SAME
// production validator (tools/application-source-review.ts) the phase relies on, and
// check the phase wiring, fixture state, and cross-plugin identity on disk.
// Run: node --test tests/tools/application-source-review.test.ts

import assert from 'node:assert/strict';
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { describe, it } from 'node:test';
import {
  ALWAYS_QUESTIONS,
  evaluateSubmission,
  isContainedPath,
  type Json,
  type JsonObject,
  LIMITS,
  measureSourceRoot,
  object,
  QUESTIONS,
  type Question,
  retainedBytes,
  scanDisallowedContent,
  selectQuestions,
  unknownForRequest,
  validate,
  validateSemantics,
} from '../../tools/application-source-review.ts';

const repoRoot = resolve(dirname(resolve(process.argv[1])), '../../../../..');
const migrate = resolve(repoRoot, 'migrate/plugins/migration-to-aws');
const advisor = resolve(repoRoot, 'advisor/plugins/aws-startup-advisor');
const schema = JSON.parse(
  readFileSync(resolve(migrate, 'skills/heroku-to-aws/references/shared/application-source-contract.schema.json'), 'utf8'),
) as JsonObject;

// --- fixtures -----------------------------------------------------------------

const workspaces: string[] = [];
function makeWorkspace(files: Record<string, string>): string {
  const ws = mkdtempSync(join(tmpdir(), 'asr-'));
  workspaces.push(ws);
  for (const [rel, content] of Object.entries(files)) {
    const abs = join(ws, rel);
    mkdirSync(dirname(abs), { recursive: true });
    writeFileSync(abs, content);
  }
  return ws;
}
process.on('exit', () => {
  for (const ws of workspaces) rmSync(ws, { recursive: true, force: true });
});

function context(overrides: Partial<Record<string, Json>> = {}): JsonObject {
  return {
    process_types: ['web'],
    configuration_names: ['PORT', 'SIGNING_SECRET'],
    postgres_attachment_present: false,
    redis_attachment_present: false,
    addon_ids: [],
    private_space_present: false,
    selected_estate_application_ids: [],
    ...overrides,
  };
}

function request(requested: readonly string[], ctx: JsonObject = context()): JsonObject {
  return {
    application: { app_id: 'app-primary', app_name: 'example-app' },
    requested_questions: [...requested],
    context: ctx,
  };
}

function inventory(applications = [{ app_id: 'app-primary', app_name: 'example-app' }]): JsonObject {
  return {
    apps: applications.map((application) => ({ ...application, space: null })),
    resources: [],
  };
}

const MISSING_SOURCE_DETAIL = 'Application source was not available for review.';

function publicationRequest(): JsonObject {
  return request(ALWAYS_QUESTIONS, context({
    process_types: [],
    configuration_names: [],
  }));
}

function publishableUnknown() {
  return {
    reviews: [{
      source_root: null,
      request: publicationRequest(),
      status: 'UNKNOWN',
      findings: unknownForRequest(ALWAYS_QUESTIONS, MISSING_SOURCE_DETAIL),
      limitations: [MISSING_SOURCE_DETAIL],
    }],
  };
}

function runtimeFramework(runtime: string, sources: Json[] = []): JsonObject {
  return {
    question: 'runtime_framework',
    status: 'PRESENT',
    value: [{ component_id: 'component-api', root: '.', runtime }],
    sources,
    limitations: [],
  };
}
function na(question: string): JsonObject {
  return { question, status: 'NOT_APPLICABLE', value: null, limitations: [] };
}
function findings(list: JsonObject[]): JsonObject {
  return { findings: list };
}

const NODE_SOURCES: Json[] = [{ path: 'package.json', line_start: 1, line_end: 3 }];

// --- question selection -------------------------------------------------------

describe('question selection from Heroku inventory', () => {
  it('always requests the 15 base questions in canonical order', () => {
    const selected = selectQuestions({
      hasInboundWebProcess: false,
      privateSpaceOrMultiApp: false,
      postgresAttached: false,
      redisAttached: false,
      ambiguousAddons: false,
    });
    assert.equal(ALWAYS_QUESTIONS.length, 15);
    assert.deepEqual(selected, [...ALWAYS_QUESTIONS]);
  });

  it('adds conditional questions per inventory signal, deduplicated and ordered', () => {
    const selected = selectQuestions({
      hasInboundWebProcess: true,
      privateSpaceOrMultiApp: true,
      postgresAttached: true,
      redisAttached: true,
      ambiguousAddons: true,
    });
    assert.deepEqual(selected, [...QUESTIONS]);
    for (const q of ['health_routes', 'webhooks', 'postgresql_extensions', 'redis_usage', 'addon_usage']) {
      assert.ok(selected.includes(q as Question), q);
    }
    // ordering matches the canonical question list
    assert.deepEqual(selected, QUESTIONS.filter((q) => selected.includes(q)));
  });
});

// --- valid contracts + fail-closed replacement --------------------------------

describe('evaluateSubmission: retain vs fail closed', () => {
  for (const runtime of ['nodejs', 'ruby', 'java']) {
    it(`retains a valid ${runtime} submission whole`, () => {
      const ws = makeWorkspace({ 'package.json': '{\n  "name": "app"\n}\n' });
      const submission = findings([runtimeFramework(runtime, NODE_SOURCES)]);
      const result = evaluateSubmission({
        schema,
        request: request(['runtime_framework']),
        submission,
        roots: [ws],
        workspaceRoot: ws,
      });
      assert.deepEqual(result.reasons, []);
      assert.equal(result.retained, true);
      assert.equal(result.findings, submission);
    });
  }

  it('fails an unsupported runtime closed to a valid all-UNKNOWN document', () => {
    const ws = makeWorkspace({ 'app.py': 'print("hello")\n' });
    const requested = ['runtime_framework', 'process_commands', 'network_listeners'];
    const submission = findings([
      runtimeFramework('python', [{ path: 'app.py' }]),
      na('process_commands'),
      na('network_listeners'),
    ]);
    const result = evaluateSubmission({
      schema,
      request: request(requested),
      submission,
      roots: [ws],
      workspaceRoot: ws,
    });
    assert.equal(result.retained, false);
    assert.match(result.reasons.join('\n'), /unsupported runtime: python/);
    assert.deepEqual(validate(schema, result.findings), []);
    assert.deepEqual(validateSemantics(request(requested), result.findings), []);
    for (const raw of result.findings.findings as Json[]) assert.equal(object(raw).status, 'UNKNOWN');
  });

  it('fails closed to one UNKNOWN per requested question when source is missing', () => {
    const ws = makeWorkspace({ 'package.json': '{}\n' });
    const submission = findings([runtimeFramework('nodejs', [{ path: 'does-not-exist.rb' }])]);
    const result = evaluateSubmission({
      schema,
      request: request(['runtime_framework']),
      submission,
      roots: [ws],
      workspaceRoot: ws,
    });
    assert.equal(result.retained, false);
    assert.match(result.reasons.join('\n'), /cited path does not resolve/);
    assert.equal((result.findings.findings as Json[]).length, 1);
    assert.equal(object((result.findings.findings as Json[])[0]).status, 'UNKNOWN');
  });

  it('fails closed when no source root is supplied or the source root is empty', () => {
    const submission = findings([runtimeFramework('nodejs')]);
    const ws = makeWorkspace({});
    const noRoot = evaluateSubmission({
      schema,
      request: request(['runtime_framework']),
      submission,
      roots: [],
      workspaceRoot: ws,
    });
    assert.equal(noRoot.retained, false);
    assert.match(noRoot.reasons.join('\n'), /exactly one source root/);

    const emptyRoot = evaluateSubmission({
      schema,
      request: request(['runtime_framework']),
      submission,
      roots: [ws],
      workspaceRoot: ws,
    });
    assert.equal(emptyRoot.retained, false);
    assert.match(emptyRoot.reasons.join('\n'), /no readable files/);
  });

  it('fails closed on a cited line range beyond the file', () => {
    const ws = makeWorkspace({ 'package.json': '{\n}\n' });
    const submission = findings([runtimeFramework('nodejs', [{ path: 'package.json', line_start: 3 }])]);
    const result = evaluateSubmission({
      schema,
      request: request(['runtime_framework']),
      submission,
      roots: [ws],
      workspaceRoot: ws,
    });
    assert.equal(result.retained, false);
  });

  it('resolves nested-app citations as workspace-relative paths', () => {
    const ws = makeWorkspace({ 'apps/web/package.json': '{}\n' });
    const root = resolve(ws, 'apps/web');
    const accepted = evaluateSubmission({
      schema,
      request: request(['runtime_framework']),
      submission: findings([runtimeFramework('nodejs', [{ path: 'apps/web/package.json' }])]),
      roots: [root],
      workspaceRoot: ws,
    });
    assert.equal(accepted.retained, true);

    const sourceRootRelative = evaluateSubmission({
      schema,
      request: request(['runtime_framework']),
      submission: findings([runtimeFramework('nodejs', [{ path: 'package.json' }])]),
      roots: [root],
      workspaceRoot: ws,
    });
    assert.equal(sourceRootRelative.retained, false);
  });

  it('fails closed on malformed, duplicate, and unrequested submissions', () => {
    const ws = makeWorkspace({ 'package.json': '{\n}\n' });
    const base = { schema, roots: [ws], workspaceRoot: ws };
    // malformed: undeclared field
    const malformed = findings([{ ...runtimeFramework('nodejs'), extra: true }]);
    assert.equal(evaluateSubmission({ ...base, request: request(['runtime_framework']), submission: malformed }).retained, false);
    // duplicate finding for the same question
    const duplicate = findings([na('build_method'), na('build_method')]);
    assert.equal(evaluateSubmission({ ...base, request: request(['build_method']), submission: duplicate }).retained, false);
    // unrequested finding
    const unrequested = findings([na('process_commands')]);
    assert.equal(evaluateSubmission({ ...base, request: request(['build_method']), submission: unrequested }).retained, false);
  });

  it('fails closed and replaces wholesale on target-bearing output', () => {
    const ws = makeWorkspace({ 'package.json': '{\n}\n' });
    const tainted = findings([{
      question: 'process_commands',
      status: 'PRESENT',
      value: [{
        process_id: 'process-web',
        component_id: 'component-api',
        type: 'web',
        name: 'web',
        command: 'Recommend deploying on AWS Fargate',
      }],
      sources: [],
      limitations: [],
    }, runtimeFramework('nodejs')]);
    const result = evaluateSubmission({
      schema,
      request: request(['process_commands', 'runtime_framework']),
      submission: tainted,
      roots: [ws],
      workspaceRoot: ws,
    });
    assert.equal(result.retained, false);
    assert.match(result.reasons.join('\n'), /target\/architecture\/cost/);
    assert.equal((result.findings.findings as Json[]).length, 2);
    for (const raw of result.findings.findings as Json[]) assert.equal(object(raw).status, 'UNKNOWN');
  });

  it('fails closed when the retained document exceeds 256 KiB', () => {
    const ws = makeWorkspace({ 'package.json': '{\n}\n' });
    const bulky = (question: string, make: (index: number) => JsonObject): JsonObject => ({
      question,
      status: 'PRESENT',
      value: Array.from({ length: 64 }, (_unused, index) => make(index)),
      sources: [],
      limitations: [],
    });
    const text = 'x'.repeat(500);
    const nm = 'y'.repeat(128);
    const list = [
      runtimeFramework('nodejs'),
      bulky('runtime_settings', () => ({
        component_id: 'component-api',
        process_ids: [],
        setting_name: nm,
        use: text,
        required: true,
        default_present: false,
        loaded_dynamically: false,
      })),
      bulky('heroku_runtime_behavior', () => ({
        component_id: 'component-api',
        process_ids: [],
        metadata_name: nm,
        use: text,
        effect: text,
      })),
      bulky('local_file_writes', () => ({
        component_id: 'component-api',
        process_ids: [],
        read_after_write: true,
        purpose: text,
        required_lifetime: nm,
        cross_instance_required: false,
      })),
      bulky('logs_telemetry', () => ({
        component_id: 'component-api',
        process_ids: [],
        signal: 'LOG',
        destination: nm,
        setting_name: nm,
      })),
      bulky('release_setup_commands', () => ({
        component_id: 'component-api',
        command: text,
        timing: nm,
        purpose: text,
      })),
    ];
    const submission = findings(list);
    const requested = ['runtime_framework', 'runtime_settings', 'heroku_runtime_behavior', 'local_file_writes', 'logs_telemetry', 'release_setup_commands'];
    assert.ok(retainedBytes(submission) > LIMITS.maxRetainedBytes);
    assert.deepEqual(validate(schema, submission), []);
    const result = evaluateSubmission({ schema, request: request(requested), submission, roots: [ws], workspaceRoot: ws });
    assert.equal(result.retained, false);
    assert.match(result.reasons.join('\n'), /retained output/);
  });
});

// --- configuration names vs credentials ---------------------------------------

describe('configuration names vs literal credentials', () => {
  it('allows configuration names such as SIGNING_SECRET', () => {
    const clean = findings([{
      question: 'runtime_settings',
      status: 'PRESENT',
      value: [{
        component_id: 'component-api',
        process_ids: [],
        setting_name: 'SIGNING_SECRET',
        use: 'HMAC signing key name',
        required: true,
        default_present: false,
        loaded_dynamically: false,
      }],
      sources: [],
      limitations: [],
    }]);
    assert.deepEqual(scanDisallowedContent(clean), []);
  });

  it('rejects a high-confidence literal credential', () => {
    const leaked = findings([{
      question: 'process_commands',
      status: 'PRESENT',
      value: [{ process_id: 'p', component_id: 'c', type: 'web', name: 'web', command: 'AKIAIOSFODNN7EXAMPLE deploy' }],
      sources: [],
      limitations: [],
    }]);
    assert.ok(scanDisallowedContent(leaked).length > 0);
  });

  it('rejects connection strings and bearer tokens', () => {
    const leaked = findings([{
      question: 'process_commands',
      status: 'PRESENT',
      value: [{
        process_id: 'p',
        component_id: 'c',
        type: 'web',
        name: 'web',
        command: 'run --database postgres://admin:supersensitive@db.example/app',
      }],
      sources: [],
      limitations: [],
    }]);
    assert.ok(scanDisallowedContent(leaked).length > 0);
  });

  it('allows source identifiers containing architecture or recommendation', () => {
    const clean = findings([{
      question: 'process_commands',
      status: 'PRESENT',
      value: [{
        process_id: 'p',
        component_id: 'c',
        type: 'worker',
        name: 'recommendation-engine',
        command: 'node recommendation-engine.js --architecture arm64',
      }],
      sources: [],
      limitations: [],
    }]);
    assert.deepEqual(scanDisallowedContent(clean), []);
  });

  it('allows an existing AWS dependency without treating it as a target recommendation', () => {
    const clean = findings([{
      question: 'external_services',
      status: 'PRESENT',
      value: [{
        dependency_id: 'database',
        component_id: 'component-api',
        kind: 'DATABASE',
        provider_reference: 'Amazon RDS',
        setting_names: ['DATABASE_URL'],
      }],
      sources: [],
      limitations: [],
    }]);
    assert.deepEqual(scanDisallowedContent(clean), []);
  });
});

// --- containment --------------------------------------------------------------

describe('path containment', () => {
  it('rejects lexically unsafe cited paths at the schema layer', () => {
    for (const path of ['/etc/passwd', 'C:/secret', '../secret', 'src/../secret', 'src//x', String.raw`src\secret`]) {
      const doc = findings([{ ...runtimeFramework('nodejs'), sources: [{ path }] }]);
      assert.ok(validate(schema, doc).length > 0, path);
    }
  });

  it('contains real paths and rejects symlinks that escape the workspace', () => {
    const outside = makeWorkspace({ 'secret.txt': 'top secret\n' });
    const ws = makeWorkspace({ 'app.rb': 'puts 1\n' });
    symlinkSync(outside, join(ws, 'escape'));
    assert.equal(isContainedPath(ws, join(ws, 'app.rb')), true);
    assert.equal(isContainedPath(ws, join(ws, 'escape')), false);

    const submission = findings([na('runtime_framework')]);
    const result = evaluateSubmission({
      schema,
      request: request(['runtime_framework']),
      submission,
      roots: [join(ws, 'escape')],
      workspaceRoot: ws,
    });
    assert.equal(result.retained, false);
    assert.match(result.reasons.join('\n'), /source root escapes workspace/);
  });

  it('measures a small source root within budget', () => {
    const ws = makeWorkspace({ 'a.js': 'a\n', 'lib/b.js': 'b\n', '.git/objects/data': 'ignored\n' });
    const measured = measureSourceRoot(ws);
    assert.equal(measured.files, 2);
    assert.equal(measured.unreadableEntries, 0);
    assert.ok(measured.withinLimits);
    assert.ok(measured.totalBytes <= LIMITS.maxTotalBytes);
  });

  it('skips internal symlinks and rejects symlink or migration-state citations', () => {
    const outside = makeWorkspace({ 'secret.txt': 'not reviewed\n' });
    const ws = makeWorkspace({
      'package.json': '{}\n',
      '.migration/0901/application-source-review.json': '{}\n',
    });
    symlinkSync(resolve(outside, 'secret.txt'), resolve(ws, 'linked-secret.txt'));
    const measured = measureSourceRoot(ws);
    assert.equal(measured.files, 1);
    assert.equal(measured.unreadableEntries, 0);

    for (const path of ['linked-secret.txt', '.migration/0901/application-source-review.json']) {
      const result = evaluateSubmission({
        schema,
        request: request(['runtime_framework']),
        submission: findings([runtimeFramework('nodejs', [{ path }])]),
        roots: [ws],
        workspaceRoot: ws,
      });
      assert.equal(result.retained, false);
      assert.match(result.reasons.join('\n'), /cited path does not resolve/);
    }
  });
});
