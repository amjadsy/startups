// application-source-review.ts — production validation and command-line tooling
// for a planned read-only Heroku application-source review.
//
// Tests import this zero-dependency implementation directly so the contract,
// filesystem, security, fail-closed, and artifact-publication behavior are
// established before any migration phase invokes it.
//
// A later change activates the workflow.

import {
  lstatSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, relative, resolve, sep } from "node:path";
import { randomUUID } from "node:crypto";

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

/** The 22 approved questions, in contract order. The reviewer cannot add or remove any. */
export const QUESTIONS = [
  "runtime_framework",
  "build_method",
  "build_time_settings",
  "process_commands",
  "runtime_settings",
  "network_listeners",
  "port_host_binding",
  "heroku_runtime_behavior",
  "native_dependencies",
  "release_setup_commands",
  "recurring_jobs",
  "health_routes",
  "local_file_writes",
  "network_protocols",
  "potential_private_endpoints",
  "logs_telemetry",
  "postgresql_extensions",
  "redis_usage",
  "external_services",
  "application_connections",
  "addon_usage",
  "webhooks",
] as const;

export type Question = (typeof QUESTIONS)[number];

/** Runtimes with validated review behavior. Anything else stays UNKNOWN (fail closed). */
export const SUPPORTED_RUNTIMES = ["ruby", "java", "nodejs", "node.js", "node"] as const;
const SUPPORTED_RUNTIME_NAMES = new Set<string>(SUPPORTED_RUNTIMES);

/** The 15 questions requested for every reviewed application. */
export const ALWAYS_QUESTIONS: readonly Question[] = [
  "runtime_framework",
  "build_method",
  "build_time_settings",
  "process_commands",
  "runtime_settings",
  "network_listeners",
  "port_host_binding",
  "heroku_runtime_behavior",
  "native_dependencies",
  "release_setup_commands",
  "recurring_jobs",
  "local_file_writes",
  "network_protocols",
  "logs_telemetry",
  "external_services",
];

/** Inventory-derived signals that add conditional questions to a request. */
export interface SelectionInput {
  hasInboundWebProcess: boolean;
  privateSpaceOrMultiApp: boolean;
  postgresAttached: boolean;
  redisAttached: boolean;
  ambiguousAddons: boolean;
}

/**
 * Select the questions for one application from accepted Heroku inventory. Always the
 * 15 base questions, plus conditionals; returned in canonical order with no
 * duplicates. The reviewer cannot alter this set.
 */
export function selectQuestions(input: SelectionInput): Question[] {
  const enabled = new Set<Question>(ALWAYS_QUESTIONS);
  if (input.hasInboundWebProcess) {
    enabled.add("health_routes");
    enabled.add("webhooks");
  }
  if (input.privateSpaceOrMultiApp) {
    enabled.add("potential_private_endpoints");
    enabled.add("application_connections");
  }
  if (input.postgresAttached) enabled.add("postgresql_extensions");
  if (input.redisAttached) enabled.add("redis_usage");
  if (input.ambiguousAddons) enabled.add("addon_usage");
  return QUESTIONS.filter((question) => enabled.has(question));
}

/** Per-application budgets enforced locally (files, bytes, retained output). */
export const LIMITS = {
  maxSourceFiles: 5000,
  maxTotalBytes: 64 * 1024 * 1024,
  maxFileBytes: 2 * 1024 * 1024,
  maxRetainedBytes: 256 * 1024,
  maxArtifactBytes: 32 * 1024 * 1024,
} as const;

export function object(value: Json): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("expected object");
  return value;
}

function same(left: Json, right: Json): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

// --- draft-07 subset validator ------------------------------------------------
// Supports the keywords the application-source contract uses: $ref, allOf, oneOf,
// not, if/then/else, const, enum, type, string/number/array/object constraints.

export function validate(node: JsonObject, value: Json, root: JsonObject = node, path = "$"): string[] {
  if (typeof node.$ref === "string") {
    if (!/^#\//.test(node.$ref)) return [`${path}: unsupported $ref ${node.$ref}`];
    let current: Json = root;
    for (const segment of node.$ref.slice(2).split("/")) current = object(current)[segment];
    return validate(object(current), value, root, path);
  }
  const errors: string[] = [];

  if (Array.isArray(node.allOf)) {
    for (const part of node.allOf) errors.push(...validate(object(part), value, root, path));
  }
  if (Array.isArray(node.oneOf)) {
    const matches = node.oneOf.filter((part) => validate(object(part), value, root, path).length === 0);
    if (matches.length !== 1) errors.push(`${path}: expected exactly one oneOf match, got ${matches.length}`);
  }
  if (node.not && validate(object(node.not), value, root, path).length === 0) {
    errors.push(`${path}: matched forbidden schema`);
  }
  if (node.if) {
    const branch = validate(object(node.if), value, root, path).length === 0 ? node.then : node.else;
    if (branch) errors.push(...validate(object(branch), value, root, path));
  }
  if ("const" in node && !same(node.const, value)) errors.push(`${path}: does not match const`);
  if (Array.isArray(node.enum) && !node.enum.some((entry) => same(entry, value))) {
    errors.push(`${path}: is not in enum`);
  }

  const type = node.type;
  const typeMatches = type === undefined
    || (type === "null" && value === null)
    || (type === "boolean" && typeof value === "boolean")
    || (type === "number" && typeof value === "number")
    || (type === "integer" && typeof value === "number" && Number.isInteger(value))
    || (type === "string" && typeof value === "string")
    || (type === "array" && Array.isArray(value))
    || (type === "object" && value !== null && typeof value === "object" && !Array.isArray(value));
  if (!typeMatches) {
    errors.push(`${path}: expected ${String(type)}`);
    return errors;
  }

  if (typeof value === "string") {
    if (typeof node.minLength === "number" && value.length < node.minLength) errors.push(`${path}: too short`);
    if (typeof node.maxLength === "number" && value.length > node.maxLength) errors.push(`${path}: too long`);
    if (typeof node.pattern === "string" && !new RegExp(node.pattern, "u").test(value)) {
      errors.push(`${path}: pattern mismatch`);
    }
  }
  if (typeof value === "number") {
    if (typeof node.minimum === "number" && value < node.minimum) errors.push(`${path}: below minimum`);
    if (typeof node.maximum === "number" && value > node.maximum) errors.push(`${path}: above maximum`);
  }
  if (Array.isArray(value)) {
    if (typeof node.minItems === "number" && value.length < node.minItems) errors.push(`${path}: too few items`);
    if (typeof node.maxItems === "number" && value.length > node.maxItems) errors.push(`${path}: too many items`);
    if (node.uniqueItems === true && new Set(value.map((entry) => JSON.stringify(entry))).size !== value.length) {
      errors.push(`${path}: duplicate items`);
    }
    if (node.items) {
      value.forEach((entry, index) => errors.push(...validate(object(node.items), entry, root, `${path}[${index}]`)));
    }
  }
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const properties = node.properties ? object(node.properties) : {};
    if (Array.isArray(node.required)) {
      for (const key of node.required) {
        if (typeof key === "string" && !(key in value)) errors.push(`${path}: missing ${key}`);
      }
    }
    if (node.additionalProperties === false) {
      for (const key of Object.keys(value)) {
        if (!(key in properties)) errors.push(`${path}: undeclared ${key}`);
      }
    }
    for (const [key, childSchema] of Object.entries(properties)) {
      if (key in value) errors.push(...validate(object(childSchema), value[key], root, `${path}.${key}`));
    }
  }
  return errors;
}

// --- semantic validation ------------------------------------------------------

function recordsByQuestion(findings: JsonObject): Map<string, JsonObject[]> {
  const result = new Map<string, JsonObject[]>();
  for (const rawFinding of findings.findings as Json[]) {
    const finding = object(rawFinding);
    result.set(finding.question as string, Array.isArray(finding.value) ? finding.value.map(object) : []);
  }
  return result;
}

/**
 * Cross-field rules the flat schema cannot express: one finding per requested
 * question, no duplicate/unrequested question, resolvable shared record references,
 * UNKNOWN carries a limitation, absence is not qualified by a source-scope
 * limitation, and source line bounds are ordered.
 */
export function validateSemantics(reviewRequest: JsonObject, answer: JsonObject): string[] {
  const errors: string[] = [];
  const requested = reviewRequest.requested_questions as string[];
  const rawFindings = answer.findings as Json[];
  const findingNames = rawFindings.map((raw) => object(raw).question as string);
  for (const question of requested) {
    if (findingNames.filter((name) => name === question).length !== 1) errors.push(`expected one finding for ${question}`);
  }
  for (const question of findingNames) {
    if (!requested.includes(question)) errors.push(`unrequested finding ${question}`);
  }
  for (const raw of rawFindings) {
    const finding = object(raw);
    const limitations = finding.limitations as JsonObject[];
    const sources = (finding.sources ?? []) as JsonObject[];
    if (
      ["PRESENT", "ABSENT_WITHIN_REVIEWED_SCOPE"].includes(finding.status as string)
      && sources.length === 0
    ) {
      errors.push(`${String(finding.question)}: ${String(finding.status)} needs source evidence`);
    }
    if (finding.status === "UNKNOWN" && limitations.length === 0) {
      errors.push(`${String(finding.question)}: UNKNOWN needs a limitation`);
    }
    if (
      finding.status === "ABSENT_WITHIN_REVIEWED_SCOPE"
      && limitations.some((item) =>
        ["SKIPPED_SOURCE", "UNREADABLE_SOURCE", "TRUNCATED_SOURCE", "DYNAMIC_SOURCE"].includes(item.kind as string)
      )
    ) errors.push(`${String(finding.question)}: absence has an incomplete scope`);
    for (const source of (finding.sources ?? []) as JsonObject[]) {
      if (
        typeof source.line_start === "number"
        && typeof source.line_end === "number"
        && source.line_end < source.line_start
      ) errors.push(`${String(finding.question)}: source line bounds are reversed`);
    }
  }

  const records = recordsByQuestion(answer);
  const ids = (question: string, key: string) => (records.get(question) ?? []).map((item) => item[key] as string);
  const componentIds = ids("runtime_framework", "component_id");
  const processIds = ids("process_commands", "process_id");
  const listenerIds = ids("network_listeners", "listener_id");
  const dependencyIds = ids("external_services", "dependency_id");
  const relationshipIds = ids("application_connections", "relationship_id");
  const components = new Set(componentIds);
  const processes = new Set(processIds);
  const listeners = new Set(listenerIds);
  const dependencies = new Set(dependencyIds);
  const estateApps = new Set(object(reviewRequest.context).selected_estate_application_ids as string[]);
  const addons = new Set(object(reviewRequest.context).addon_ids as string[]);

  for (const [question, questionRecords] of records) {
    for (const record of questionRecords) {
      for (const key of ["component_id", "caller_component_id"]) {
        if (typeof record[key] === "string" && !components.has(record[key])) errors.push(`${question}: broken ${key}`);
      }
      for (const key of ["process_id", "process_ids", "caller_process_ids"]) {
        const references = typeof record[key] === "string" ? [record[key]] : (record[key] ?? []) as Json[];
        for (const reference of references) {
          if (typeof reference === "string" && !processes.has(reference)) errors.push(`${question}: broken ${key}`);
        }
      }
      if (typeof record.listener_id === "string" && !listeners.has(record.listener_id)) {
        errors.push(`${question}: broken listener_id`);
      }
      if (typeof record.callee_application_id === "string" && !estateApps.has(record.callee_application_id)) {
        errors.push(`${question}: broken callee_application_id`);
      }
      if (typeof record.inventory_addon_id === "string" && !addons.has(record.inventory_addon_id)) {
        errors.push(`${question}: broken inventory_addon_id`);
      }
      if (record.reference_kind === "DEPENDENCY" && !dependencies.has(record.reference_id as string)) {
        errors.push(`${question}: broken dependency reference_id`);
      }
      if (record.reference_kind === "APPLICATION" && !estateApps.has(record.reference_id as string)) {
        errors.push(`${question}: broken application reference_id`);
      }
    }
  }

  for (
    const [label, valuesToCheck] of [
      ["component", componentIds],
      ["process", processIds],
      ["listener", listenerIds],
      ["dependency", dependencyIds],
      ["relationship", relationshipIds],
    ] as const
  ) {
    if (new Set(valuesToCheck).size !== valuesToCheck.length) errors.push(`duplicate ${label} id`);
  }
  return errors;
}

function validateRuntimeSupport(reviewRequest: JsonObject, answer: JsonObject): string[] {
  const requested = reviewRequest.requested_questions as string[];
  if (!requested.includes("runtime_framework")) return [];

  const raw = (answer.findings as Json[]).find(
    (finding) => object(finding).question === "runtime_framework",
  );
  if (!raw) return ["runtime support could not be established"];

  const finding = object(raw);
  if (finding.status !== "PRESENT" || !Array.isArray(finding.value)) {
    return ["runtime support could not be established"];
  }

  const unsupported = finding.value
    .map((record) => String(object(record).runtime).trim().toLowerCase())
    .filter((runtime) => !SUPPORTED_RUNTIME_NAMES.has(runtime));
  return unsupported.length === 0
    ? []
    : [`unsupported runtime: ${[...new Set(unsupported)].join(", ")}`];
}

// --- disallowed-content scanning ----------------------------------------------
// The reviewer records configuration NAMES, never values. It also never emits a
// target decision, architecture, sizing, or cost. These high-confidence patterns
// catch a submission that leaked a literal credential or a recommendation.

const CREDENTIAL_PATTERNS: RegExp[] = [
  /\bAKIA[0-9A-Z]{16}\b/, // AWS access key id
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/, // PEM private key
  /\bxox[baprs]-[0-9A-Za-z-]{10,}\b/, // Slack token
  /\bgh[pousr]_[0-9A-Za-z]{20,}\b/, // GitHub token
  /\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*\b/i, // bearer token
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/, // JWT
  /\b[a-z][a-z0-9+.-]*:\/\/[^/\s:@]+:[^/\s@]+@/i, // URI userinfo
  /\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*["']?[^\s"']{8,}/i, // literal assignment
  /--?(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)(?:=|\s+)["']?[^\s"']{8,}/i, // CLI literal
];

const TARGET_PATTERNS: RegExp[] = [
  /\b(?:target|destination|deploy(?:ment)?|migrat(?:e|ion))\b.{0,80}\b(?:elastic beanstalk|fargate|amazon rds\b|amazon aurora|elasticache|amazon eks|amazon msk|app runner)\b/i,
  /\b(?:recommend|recommends|recommended)\s+(?:using|use|deploying|deploy|moving|move|migrating|migrate|to|on)\b/i,
  /\b(?:recommended|proposed)\s+architecture\b/i,
  /(?:\$\s?\d|\bmonthly cost\b|\bUSD\b|\bper month\b)/i,
];

function walkStrings(value: Json, visit: (text: string) => void): void {
  const pending: Json[] = [value];
  while (pending.length > 0) {
    const current = pending.pop() as Json;
    if (typeof current === "string") visit(current);
    else if (Array.isArray(current)) pending.push(...current);
    else if (current !== null && typeof current === "object") pending.push(...Object.values(current));
  }
}

function scanCredentialContent(value: Json): string[] {
  const reasons: string[] = [];
  walkStrings(value, (text) => {
    for (const pattern of CREDENTIAL_PATTERNS) {
      if (pattern.test(text)) reasons.push(`high-confidence credential in output: ${pattern.source}`);
    }
  });
  return reasons;
}

/** Returns the reasons a submission's string values are disallowed (empty when clean). */
export function scanDisallowedContent(answer: JsonObject): string[] {
  const content = answer.findings ?? null;
  const reasons = scanCredentialContent(content);
  walkStrings(content, (text) => {
    for (const pattern of TARGET_PATTERNS) {
      if (pattern.test(text)) reasons.push(`target/architecture/cost content in output: ${pattern.source}`);
    }
  });
  return reasons;
}

/** Retained output must be at most 256 KiB per application. */
export function retainedBytes(answer: JsonObject): number {
  return new TextEncoder().encode(JSON.stringify(answer)).length;
}

// --- filesystem containment + budgets -----------------------------------------

/** True when `candidateAbs` resolves (through symlinks) to inside `workspaceAbs`. */
export function isContainedPath(workspaceAbs: string, candidateAbs: string): boolean {
  try {
    const workspaceReal = realpathSync(workspaceAbs);
    const candidateReal = realpathSync(candidateAbs);
    return candidateReal === workspaceReal || candidateReal.startsWith(workspaceReal + sep);
  } catch {
    return false;
  }
}

function pathTraversesSymlink(workspaceAbs: string, candidateAbs: string): boolean {
  const lexical = relative(resolve(workspaceAbs), resolve(candidateAbs));
  if (lexical === "" || lexical.startsWith(`..${sep}`) || lexical === "..") return false;
  let current = resolve(workspaceAbs);
  for (const segment of lexical.split(sep)) {
    current = resolve(current, segment);
    try {
      if (lstatSync(current).isSymbolicLink()) return true;
    } catch {
      return false;
    }
  }
  return false;
}

export interface RootMeasurement {
  files: number;
  totalBytes: number;
  maxFileBytes: number;
  unreadableEntries: number;
  withinLimits: boolean;
}

/** Walk a source root counting regular files and bytes; never follows symlinked dirs. */
export function measureSourceRoot(rootAbs: string): RootMeasurement {
  let files = 0;
  let totalBytes = 0;
  let maxFileBytes = 0;
  let unreadableEntries = 0;
  const stack = [rootAbs];
  while (stack.length > 0) {
    const dir = stack.pop() as string;
    let entries: string[];
    try {
      entries = readdirSync(dir);
    } catch {
      unreadableEntries += 1;
      continue;
    }
    for (const entry of entries) {
      if ([".git", ".migration", "node_modules", ".venv"].includes(entry)) continue;
      const child = resolve(dir, entry);
      let info;
      try {
        info = lstatSync(child);
      } catch {
        unreadableEntries += 1;
        continue;
      }
      if (info.isSymbolicLink()) {
        continue; // never follow or count symlinked source entries
      }
      if (info.isDirectory()) {
        if (dir === rootAbs && entry === ".git") continue;
        stack.push(child);
      } else if (info.isFile()) {
        files += 1;
        totalBytes += info.size;
        if (info.size > maxFileBytes) maxFileBytes = info.size;
        if (
          files > LIMITS.maxSourceFiles
          || totalBytes > LIMITS.maxTotalBytes
          || maxFileBytes > LIMITS.maxFileBytes
        ) {
          return { files, totalBytes, maxFileBytes, unreadableEntries, withinLimits: false };
        }
      }
    }
  }
  const withinLimits = files <= LIMITS.maxSourceFiles
    && totalBytes <= LIMITS.maxTotalBytes
    && maxFileBytes <= LIMITS.maxFileBytes;
  return { files, totalBytes, maxFileBytes, unreadableEntries, withinLimits };
}

function citationResolves(roots: string[], workspaceAbs: string, source: JsonObject): boolean {
  const relPath = source.path;
  if (typeof relPath !== "string") return false;
  if (relPath.split("/").some((segment) => [".git", ".migration", "node_modules", ".venv"].includes(segment))) {
    return false;
  }
  const candidate = resolve(workspaceAbs, relPath);
  if (!isContainedPath(workspaceAbs, candidate)) return false;
  if (!roots.some((root) => isContainedPath(root, candidate))) return false;
  if (pathTraversesSymlink(workspaceAbs, candidate)) return false;
  let info;
  try {
    info = lstatSync(candidate);
  } catch {
    return false;
  }
  if (!info.isFile() || info.isSymbolicLink()) return false;
  const start = source.line_start;
  const end = source.line_end;
  if (typeof start === "number" || typeof end === "number") {
    const text = readFileSync(candidate, "utf8");
    const newlineCount = text.match(/\r\n|\r|\n/gu)?.length ?? 0;
    const lineCount = text.length === 0
      ? 0
      : newlineCount + (/(\r\n|\r|\n)$/u.test(text) ? 0 : 1);
    if (typeof start === "number" && start > lineCount) return false;
    if (typeof end === "number" && end > lineCount) return false;
  }
  return true;
}

/** Validate source roots before dispatching a reviewer. */
export function validateSourceRoots(workspaceAbs: string, roots: string[]): string[] {
  const reasons: string[] = [];
  if (roots.length !== 1) return ["exactly one source root is required per application"];
  for (const root of roots) {
    if (!isContainedPath(workspaceAbs, root)) {
      reasons.push(`source root escapes workspace: ${root}`);
      continue;
    }
    if (lstatSync(root).isSymbolicLink()) {
      reasons.push(`source root is a symlink: ${root}`);
      continue;
    }
    const measurement = measureSourceRoot(root);
    if (measurement.unreadableEntries > 0) reasons.push(`source root contains unreadable entries: ${root}`);
    else if (measurement.files === 0) reasons.push(`source root contains no readable files: ${root}`);
    else if (!measurement.withinLimits) reasons.push(`source root exceeds file/byte budget: ${root}`);
  }
  return reasons;
}

// --- deterministic fail-closed replacement ------------------------------------

const VALIDATION_UNKNOWN_DETAIL = "Source review could not be validated.";
const UNKNOWN_REASONS: Record<string, string> = {
  missing_source: "Application source was not available for review.",
  review_interrupted: "Application source review was interrupted.",
  review_unavailable: "Application source review was unavailable.",
  review_over_budget: "Application source review exceeded an execution limit.",
};
const ALLOWED_UNKNOWN_DETAILS = new Set([VALIDATION_UNKNOWN_DETAIL, ...Object.values(UNKNOWN_REASONS)]);

/** One deterministic UNKNOWN finding per requested question — the fail-closed output. */
export function unknownForRequest(requestedQuestions: readonly string[], detail: string): JsonObject {
  return {
    findings: requestedQuestions.map((question) => ({
      question,
      status: "UNKNOWN",
      value: null,
      sources: [],
      limitations: [{ kind: "OTHER", detail }],
    })),
  };
}

function jsonObject(value: Json): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function hasExactKeys(value: JsonObject, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  return actual.length === keys.length && actual.every((key, index) => key === [...keys].sort()[index]);
}

function validRelativeRoot(value: string): boolean {
  if (value === ".") return true;
  if (value.length === 0 || value.length > 500 || value.startsWith("/") || /^[A-Za-z]:/u.test(value)) return false;
  if (value.includes("\\") || value.includes("//")) return false;
  return value.split("/").every((segment) => segment !== "" && segment !== "." && segment !== "..");
}

function validateRequest(schema: JsonObject, request: JsonObject, path: string): string[] {
  const errors = validate(schema, request, schema, path);
  if (errors.length > 0) return errors;

  const context = jsonObject(request.context);
  const names = context?.configuration_names;
  if (
    !Array.isArray(names)
    || names.some((name) => typeof name !== "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/u.test(name))
  ) {
    errors.push(`${path}: configuration_names must contain names only`);
  }
  errors.push(...scanCredentialContent(request).map((error) => `${path}: ${error}`));
  return errors;
}

/**
 * Validate the final wrapper before it becomes the canonical artifact. This repeats
 * retained-submission validation so the controller cannot change accepted findings.
 */
export function validateReviewArtifact(
  schema: JsonObject,
  artifact: JsonObject,
  workspaceRoot: string,
  expectedRequests: readonly JsonObject[],
): string[] {
  const errors: string[] = [];
  if (!hasExactKeys(artifact, ["reviews"]) || !Array.isArray(artifact.reviews)) {
    return ["artifact must contain only a reviews array"];
  }
  if (artifact.reviews.length !== expectedRequests.length) {
    errors.push(`artifact has ${artifact.reviews.length} reviews; expected ${expectedRequests.length}`);
  }

  const appIds = new Set<string>();
  for (const [index, raw] of artifact.reviews.entries()) {
    const entry = jsonObject(raw);
    const at = `reviews[${index}]`;
    if (!entry || !hasExactKeys(entry, ["findings", "limitations", "request", "source_root", "status"])) {
      errors.push(`${at}: invalid entry shape`);
      continue;
    }
    const request = jsonObject(entry.request);
    const findings = jsonObject(entry.findings);
    if (!request || !findings) {
      errors.push(`${at}: request and findings must be objects`);
      continue;
    }
    const requestErrors = validateRequest(schema, request, `${at}.request`);
    const findingErrors = validate(schema, findings, schema, `${at}.findings`);
    errors.push(...requestErrors, ...findingErrors);
    if (requestErrors.length > 0 || findingErrors.length > 0) continue;
    errors.push(...validateSemantics(request, findings).map((error) => `${at}: ${error}`));

    const application = jsonObject(request.application);
    const appId = application?.app_id;
    if (typeof appId !== "string" || appIds.has(appId)) errors.push(`${at}: duplicate or missing app_id`);
    else appIds.add(appId);
    const expected = expectedRequests[index];
    if (!expected || !same(request, expected)) {
      errors.push(`${at}: request content or inventory order does not match`);
    }

    const limitations = Array.isArray(entry.limitations) ? entry.limitations : [];
    if (
      !Array.isArray(entry.limitations)
      || limitations.some((item) => typeof item !== "string" || item.length === 0 || item.length > 500)
    ) errors.push(`${at}: limitations must be concise strings`);
    errors.push(...scanDisallowedContent({ findings: limitations }).map((error) => `${at}: ${error}`));

    const sourceRoot = entry.source_root;
    if (sourceRoot !== null && (typeof sourceRoot !== "string" || !validRelativeRoot(sourceRoot))) {
      errors.push(`${at}: source_root must be workspace-relative or null`);
    }

    if (entry.status === "RETAINED") {
      if (typeof sourceRoot !== "string") errors.push(`${at}: RETAINED requires a source_root`);
      else {
        const result = evaluateSubmission({
          schema,
          request,
          submission: findings,
          workspaceRoot,
          roots: [resolve(workspaceRoot, sourceRoot)],
        });
        errors.push(...result.reasons.map((reason) => `${at}: ${reason}`));
      }
      if (limitations.length !== 0) errors.push(`${at}: RETAINED cannot have limitations`);
    } else if (entry.status === "UNKNOWN") {
      if (limitations.length === 0) errors.push(`${at}: UNKNOWN needs a limitation`);
      const requested = request.requested_questions as string[];
      const findingList = findings.findings as Json[];
      const first = findingList.length > 0 ? jsonObject(findingList[0]) : null;
      const firstLimitation = first && Array.isArray(first.limitations)
        ? jsonObject(first.limitations[0] as Json)
        : null;
      const detail = firstLimitation?.detail;
      if (
        typeof detail !== "string"
        || !ALLOWED_UNKNOWN_DETAILS.has(detail)
        || !same(findings, unknownForRequest(requested, detail))
      ) errors.push(`${at}: UNKNOWN findings must be a canonical fail-closed replacement`);
      if (retainedBytes(findings) > LIMITS.maxRetainedBytes) errors.push(`${at}: findings exceed retained budget`);
      errors.push(...scanDisallowedContent(findings).map((error) => `${at}: ${error}`));
    } else {
      errors.push(`${at}: status must be RETAINED or UNKNOWN`);
    }
  }
  return errors;
}

export interface SubmissionContext {
  schema: JsonObject;
  request: JsonObject;
  submission: JsonObject;
  roots: string[];
  workspaceRoot: string;
}

export interface SubmissionResult {
  retained: boolean;
  findings: JsonObject;
  reasons: string[];
}

/**
 * Validate a COMPLETE reviewer submission for one application and return either the
 * retained findings (when every check passes) or one deterministic UNKNOWN finding
 * per requested question (fail closed). No partial acceptance.
 */
export function evaluateSubmission(ctx: SubmissionContext): SubmissionResult {
  const reasons: string[] = [];
  reasons.push(...validateRequest(ctx.schema, ctx.request, "request").map((error) => `request ${error}`));
  reasons.push(...validate(ctx.schema, ctx.submission, ctx.schema, "findings").map((error) => `findings ${error}`));
  if (reasons.length === 0) {
    reasons.push(...validateSemantics(ctx.request, ctx.submission));
    reasons.push(...validateRuntimeSupport(ctx.request, ctx.submission));
  }
  reasons.push(...scanDisallowedContent(ctx.submission));

  const bytes = retainedBytes(ctx.submission);
  if (bytes > LIMITS.maxRetainedBytes) reasons.push(`retained output ${bytes} bytes exceeds ${LIMITS.maxRetainedBytes}`);

  reasons.push(...validateSourceRoots(ctx.workspaceRoot, ctx.roots));

  if (reasons.length === 0) {
    for (const raw of ctx.submission.findings as Json[]) {
      const finding = object(raw);
      for (const source of (finding.sources ?? []) as JsonObject[]) {
        if (!citationResolves(ctx.roots, ctx.workspaceRoot, source)) {
          reasons.push(`${String(finding.question)}: cited path does not resolve within workspace`);
        }
      }
    }
  }

  if (reasons.length === 0) return { retained: true, findings: ctx.submission, reasons: [] };
  const rawRequested = ctx.request.requested_questions;
  const requested = Array.isArray(rawRequested)
    ? rawRequested.filter((question): question is string => typeof question === "string")
    : [];
  return {
    retained: false,
    findings: unknownForRequest(requested, VALIDATION_UNKNOWN_DETAIL),
    reasons,
  };
}

// --- CLI ---------------------------------------------------------------------

const USAGE = [
  "usage:",
  "  node application-source-review.ts questions <selection-signals.json>",
  "  node application-source-review.ts check-roots <workspace-root> <roots.json>",
  "  node application-source-review.ts unknown <schema.json> <request.json> <reason>",
  "  node application-source-review.ts validate <schema.json> <request.json> <submission.json> <workspace-root> <roots.json>",
  "  node application-source-review.ts publish-artifact <schema.json> <inventory.json> <candidate.json> <workspace-root> <output.json>",
].join("\n");

export interface CliResult {
  exitCode: number;
  stdout?: string;
  stderr?: string;
}

function readJsonObject(path: string, maxBytes = LIMITS.maxFileBytes): JsonObject {
  if (statSync(path).size > maxBytes) throw new Error(`${path} exceeds ${maxBytes} bytes`);
  return object(JSON.parse(readFileSync(path, "utf8")) as Json);
}

function removeIfPresent(path: string): void {
  try {
    unlinkSync(path);
  } catch {
    // The transient or stale artifact may not exist.
  }
}

function readAndDeleteJsonObject(path: string, maxBytes: number): JsonObject {
  try {
    return readJsonObject(path, maxBytes);
  } finally {
    removeIfPresent(path);
  }
}

function expectedRequestsFromInventory(path: string): JsonObject[] {
  const inventory = readJsonObject(path, LIMITS.maxArtifactBytes);
  if (!Array.isArray(inventory.apps)) throw new Error("inventory must contain an apps array");
  if (!Array.isArray(inventory.resources)) throw new Error("inventory must contain a resources array");
  const apps = inventory.apps.map((raw, index) => {
    const app = jsonObject(raw);
    if (!app || typeof app.app_id !== "string" || typeof app.app_name !== "string") {
      throw new Error(`inventory apps[${index}] has no app_id/app_name`);
    }
    return app;
  });
  const resources = inventory.resources.map((raw, index) => {
    const resource = jsonObject(raw);
    if (!resource || typeof resource.resource_type !== "string" || typeof resource.heroku_app !== "string") {
      throw new Error(`inventory resources[${index}] has no resource_type/heroku_app`);
    }
    return resource;
  });
  const unique = (values: string[]) => [...new Set(values)];

  return apps.map((app) => {
    const appResources = resources.filter((resource) => resource.heroku_app === app.app_name);
    const formations = appResources.filter((resource) => resource.resource_type === "formation");
    const addons = appResources.filter((resource) => resource.resource_type === "addon");
    const configuration = appResources.find((resource) => resource.resource_type === "config");
    const processTypes = unique(formations.flatMap((resource) => {
      const config = jsonObject(resource.config);
      return typeof config?.process_type === "string" ? [config.process_type] : [];
    }));
    const configurationNames = unique((() => {
      const config = configuration ? jsonObject(configuration.config) : null;
      return Array.isArray(config?.config_var_keys)
        ? config.config_var_keys.filter((name): name is string => typeof name === "string")
        : [];
    })());
    const addonServices = addons.map((resource) => {
      const config = jsonObject(resource.config);
      return typeof config?.addon_service === "string" ? config.addon_service : "";
    });
    const addonIds = unique(addons.flatMap((resource) =>
      typeof resource.resource_id === "string" ? [resource.resource_id] : []
    ));
    const postgresAttached = addonServices.includes("heroku-postgresql");
    const redisAttached = addonServices.includes("heroku-redis");
    const privateSpacePresent = app.space !== null && app.space !== undefined;
    const selection: SelectionInput = {
      hasInboundWebProcess: processTypes.includes("web"),
      privateSpaceOrMultiApp: privateSpacePresent || apps.length > 1,
      postgresAttached,
      redisAttached,
      ambiguousAddons: addonServices.some((service) =>
        service !== "heroku-postgresql" && service !== "heroku-redis"
      ),
    };
    return {
      application: { app_id: app.app_id, app_name: app.app_name },
      requested_questions: selectQuestions(selection),
      context: {
        process_types: processTypes,
        configuration_names: configurationNames,
        postgres_attachment_present: postgresAttached,
        redis_attachment_present: redisAttached,
        addon_ids: addonIds,
        private_space_present: privateSpacePresent,
        selected_estate_application_ids: apps
          .filter((other) => other.app_id !== app.app_id)
          .map((other) => other.app_id as string),
      },
    };
  });
}

function safeTransientFile(workspaceRoot: string, path: string, expectedName: string): string | null {
  const absolute = resolve(path);
  if (
    basename(absolute) !== expectedName
    || !isContainedPath(workspaceRoot, absolute)
    || pathTraversesSymlink(workspaceRoot, absolute)
  ) return null;
  try {
    const info = lstatSync(absolute);
    return info.isFile() && !info.isSymbolicLink() ? absolute : null;
  } catch {
    return null;
  }
}

function readSelectionInput(path: string): SelectionInput {
  const value = readJsonObject(path, 4096);
  const keys = [
    "ambiguousAddons",
    "hasInboundWebProcess",
    "postgresAttached",
    "privateSpaceOrMultiApp",
    "redisAttached",
  ];
  if (!hasExactKeys(value, keys) || keys.some((key) => typeof value[key] !== "boolean")) {
    throw new Error("selection signals must contain exactly five boolean fields");
  }
  return value as unknown as SelectionInput;
}

function readRoots(path: string, workspaceRoot: string): { roots: string[]; reasons: string[] } {
  if (statSync(path).size > 4096) throw new Error("roots file exceeds 4096 bytes");
  const value = JSON.parse(readFileSync(path, "utf8")) as Json;
  if (!Array.isArray(value) || value.some((root) => typeof root !== "string")) {
    throw new Error("roots file must be a JSON string array");
  }
  const relativeRoots = value as string[];
  const reasons = relativeRoots
    .filter((root) => !validRelativeRoot(root))
    .map((root) => `source root is not workspace-relative: ${root}`);
  return {
    roots: reasons.length === 0 ? relativeRoots.map((root) => resolve(workspaceRoot, root)) : [],
    reasons,
  };
}

function rejectedSubmission(request: JsonObject, reasons: string[]): CliResult {
  return {
    exitCode: 1,
    stdout: JSON.stringify({
      retained: false,
      findings: unknownForRequest(
        request.requested_questions as string[],
        "Source review could not be validated.",
      ),
      reasons,
    }, null, 2),
  };
}

/** Execute one CLI command without exiting, so tests can exercise the runtime path. */
export function runCli(argv: readonly string[]): CliResult {
  const command = argv[0];
  if (command === "questions") {
    if (argv.length !== 2) return { exitCode: 2, stderr: USAGE };
    try {
      return { exitCode: 0, stdout: JSON.stringify(selectQuestions(readSelectionInput(argv[1])), null, 2) };
    } catch (error) {
      return { exitCode: 2, stderr: `question selection did not run: ${error instanceof Error ? error.message : String(error)}` };
    }
  }

  if (command === "check-roots") {
    if (argv.length !== 3) return { exitCode: 2, stderr: USAGE };
    try {
      const [, workspaceRoot, rootsPath] = argv;
      const workspaceAbs = resolve(workspaceRoot);
      const rootInput = readRoots(rootsPath, workspaceAbs);
      const reasons = [...rootInput.reasons, ...validateSourceRoots(workspaceAbs, rootInput.roots)];
      return {
        exitCode: reasons.length === 0 ? 0 : 1,
        stdout: JSON.stringify({ valid: reasons.length === 0, reasons }, null, 2),
      };
    } catch (error) {
      return { exitCode: 2, stderr: `source-root preflight did not run: ${error instanceof Error ? error.message : String(error)}` };
    }
  }

  if (command === "unknown") {
    if (argv.length !== 4 || !Object.keys(UNKNOWN_REASONS).includes(argv[3])) return { exitCode: 2, stderr: USAGE };
    try {
      const [, schemaPath, requestPath, reason] = argv;
      const schema = readJsonObject(schemaPath);
      const request = readJsonObject(requestPath, LIMITS.maxRetainedBytes);
      const requestErrors = validateRequest(schema, request, "request");
      if (requestErrors.length > 0) {
        return { exitCode: 2, stderr: `source-review request is invalid: ${requestErrors.join("; ")}` };
      }
      return {
        exitCode: 0,
        stdout: JSON.stringify(
          unknownForRequest(request.requested_questions as string[], UNKNOWN_REASONS[reason]),
          null,
          2,
        ),
      };
    } catch (error) {
      return { exitCode: 2, stderr: `UNKNOWN findings were not generated: ${error instanceof Error ? error.message : String(error)}` };
    }
  }

  if (command === "validate") {
    if (argv.length !== 6) return { exitCode: 2, stderr: USAGE };
    const workspaceAbs = resolve(argv[4]);
    const submissionPath = safeTransientFile(
      workspaceAbs,
      argv[3],
      ".source-review-candidate.json",
    );
    if (!submissionPath) return { exitCode: 2, stderr: "submission must be the contained source-review transient" };
    try {
      const [, schemaPath, requestPath, , workspaceRoot, rootsPath] = argv;
      if (resolve(workspaceRoot) !== workspaceAbs) return { exitCode: 2, stderr: USAGE };
      const schema = readJsonObject(schemaPath);
      const request = readJsonObject(requestPath, LIMITS.maxRetainedBytes);
      const requestErrors = validateRequest(schema, request, "request");
      if (requestErrors.length > 0) {
        removeIfPresent(submissionPath);
        return { exitCode: 2, stderr: `source-review request is invalid: ${requestErrors.join("; ")}` };
      }
      const rootInput = readRoots(rootsPath, workspaceAbs);
      let submission: JsonObject;
      try {
        submission = readAndDeleteJsonObject(submissionPath, LIMITS.maxRetainedBytes);
      } catch (error) {
        return rejectedSubmission(request, [
          `reviewer submission could not be read: ${error instanceof Error ? error.message : String(error)}`,
        ]);
      }
      let result: SubmissionResult;
      try {
        result = evaluateSubmission({
          schema,
          request,
          submission,
          workspaceRoot: workspaceAbs,
          roots: rootInput.roots,
        });
      } catch (error) {
        return rejectedSubmission(request, [
          `reviewer submission could not be validated: ${error instanceof Error ? error.message : String(error)}`,
        ]);
      }
      if (rootInput.reasons.length > 0) {
        result.retained = false;
        result.reasons.unshift(...rootInput.reasons);
        result.findings = unknownForRequest(request.requested_questions as string[], VALIDATION_UNKNOWN_DETAIL);
      }
      return {
        exitCode: result.retained ? 0 : 1,
        stdout: JSON.stringify(result, null, 2),
      };
    } catch (error) {
      removeIfPresent(submissionPath);
      return {
        exitCode: 2,
        stderr: `source-review validator did not run: ${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  if (command === "publish-artifact") {
    if (argv.length !== 6) return { exitCode: 2, stderr: USAGE };
    let temporary = "";
    let candidateAbs = "";
    try {
      const [, schemaPath, inventoryPath, candidatePath, workspaceRoot, outputPath] = argv;
      const workspaceAbs = resolve(workspaceRoot);
      const outputAbs = resolve(outputPath);
      if (
        basename(outputAbs) !== "application-source-review.json"
        || !isContainedPath(workspaceAbs, dirname(outputAbs))
        || pathTraversesSymlink(workspaceAbs, dirname(outputAbs))
      ) {
        return { exitCode: 2, stderr: "output must be the contained canonical source-review artifact" };
      }
      removeIfPresent(outputAbs);
      const safeCandidate = safeTransientFile(
        workspaceAbs,
        candidatePath,
        ".application-source-review.candidate.json",
      );
      if (!safeCandidate) return { exitCode: 2, stderr: "candidate must be the contained source-review transient" };
      candidateAbs = safeCandidate;
      const schema = readJsonObject(schemaPath);
      const expectedRequests = expectedRequestsFromInventory(inventoryPath);
      let artifact: JsonObject;
      try {
        artifact = readAndDeleteJsonObject(candidateAbs, LIMITS.maxArtifactBytes);
      } catch (error) {
        return {
          exitCode: 1,
          stdout: JSON.stringify({
            published: false,
            reasons: [`candidate artifact could not be read: ${error instanceof Error ? error.message : String(error)}`],
          }, null, 2),
        };
      }
      let reasons: string[];
      try {
        reasons = validateReviewArtifact(schema, artifact, workspaceAbs, expectedRequests);
      } catch (error) {
        reasons = [`candidate artifact could not be validated: ${error instanceof Error ? error.message : String(error)}`];
      }
      if (reasons.length > 0) {
        return { exitCode: 1, stdout: JSON.stringify({ published: false, reasons }, null, 2) };
      }
      temporary = `${outputAbs}.tmp-${process.pid}-${randomUUID()}`;
      writeFileSync(temporary, `${JSON.stringify(artifact, null, 2)}\n`, { flag: "wx", mode: 0o600 });
      renameSync(temporary, outputAbs);
      return { exitCode: 0, stdout: JSON.stringify({ published: true }, null, 2) };
    } catch (error) {
      if (candidateAbs !== "") removeIfPresent(candidateAbs);
      if (temporary !== "") {
        removeIfPresent(temporary);
      }
      return { exitCode: 2, stderr: `artifact publisher did not run: ${error instanceof Error ? error.message : String(error)}` };
    }
  }

  return { exitCode: 2, stderr: USAGE };
}

// Run only when invoked directly, not when imported by tests.
const invokedPath = process.argv[1] ?? "";
if (invokedPath.endsWith("application-source-review.ts")) {
  const result = runCli(process.argv.slice(2));
  if (result.stdout) console.log(result.stdout);
  if (result.stderr) console.error(result.stderr);
  process.exit(result.exitCode);
}
