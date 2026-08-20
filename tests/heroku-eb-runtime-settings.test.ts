import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

const PLUGINS = [
  "migrate/plugins/migration-to-aws",
  "advisor/plugins/aws-startup-advisor",
];

const GENERATE = "skills/heroku-to-aws/references/phases/generate";

function read(plugin: string, file: string): string {
  return readFileSync(join(plugin, GENERATE, file), "utf8");
}

function blocks(source: string, header: string): string[] {
  const found: string[] = [];
  let searchFrom = 0;

  while (true) {
    const start = source.indexOf(header, searchFrom);
    if (start === -1) return found;

    const open = source.indexOf("{", start);
    let depth = 0;
    let closed = false;
    for (let cursor = open; cursor < source.length; cursor++) {
      if (source[cursor] === "{") depth++;
      if (source[cursor] === "}") depth--;
      if (depth === 0) {
        found.push(source.slice(start, cursor + 1));
        searchFrom = cursor + 1;
        closed = true;
        break;
      }
    }
    assert.ok(closed, `unclosed block: ${header}`);
  }
}

function onlyBlock(source: string, header: string): string {
  const found = blocks(source, header);
  assert.equal(found.length, 1, `expected one block: ${header}`);
  return found[0];
}

function blockBody(block: string): string {
  return block.slice(block.indexOf("{") + 1, -1);
}

function variableBlock(terraform: string, name: string): string {
  return onlyBlock(terraform, `variable "${name}" {`);
}

function attribute(block: string, name: string): string | undefined {
  return blockBody(block).match(
    new RegExp(`^\\s*${name}\\s*=\\s*(.+)$`, "m"),
  )?.[1].trim();
}

function settingExpression(
  terraform: string,
  settingName: string,
): string {
  const matching = blocks(terraform, "setting {").filter(
    (setting) => attribute(setting, "name") === `"${settingName}"`,
  );
  assert.equal(matching.length, 1, `expected one ${settingName} setting`);
  const value = attribute(matching[0], "value");
  assert.ok(value, `missing value in ${settingName} setting`);
  return value;
}

function terraformFixture(terraform: string): string {
  const directory = mkdtempSync(join(tmpdir(), "eb-runtime-settings-"));
  writeFileSync(
    join(directory, "main.tf"),
    `${variableBlock(terraform, "eb_application_port")}

${variableBlock(terraform, "eb_health_check_path")}

output "runtime_settings" {
  value = {
    PORT            = ${settingExpression(terraform, "PORT")}
    HealthCheckPath = ${settingExpression(terraform, "HealthCheckPath")}
  }
}
`,
  );
  return directory;
}

function runTerraform(directory: string, args: string[]) {
  return spawnSync("terraform", args, {
    cwd: directory,
    encoding: "utf8",
    env: {
      ...process.env,
      TF_DATA_DIR: join(directory, ".terraform-data"),
      TF_IN_AUTOMATION: "1",
    },
  });
}

function resultOutput(result: ReturnType<typeof runTerraform>): string {
  return `${result.stdout}\n${result.stderr}`;
}

describe("Heroku Elastic Beanstalk runtime settings", () => {
  it("keeps both plugin generation surfaces byte-identical", () => {
    const files = readdirSync(join(PLUGINS[0], GENERATE)).sort();
    assert.deepEqual(
      files,
      readdirSync(join(PLUGINS[1], GENERATE)).sort(),
    );
    for (const file of files) {
      assert.equal(read(PLUGINS[0], file), read(PLUGINS[1], file), file);
    }
  });

  for (const plugin of PLUGINS) {
    it(`${plugin} requires explicit values and preserves them unchanged`, () => {
      const terraform = read(plugin, "generate-terraform.md");

      for (
        const variable of ["eb_application_port", "eb_health_check_path"]
      ) {
        const body = blockBody(variableBlock(terraform, variable));
        assert.match(body, /\btype\s+=\s+string\b/);
        assert.doesNotMatch(
          body,
          /\bdefault\s*=/,
          `${variable} must remain required so non-interactive planning fails when it is omitted`,
        );
      }

      assert.equal(settingExpression(terraform, "PORT"), "var.eb_application_port");
      assert.equal(
        settingExpression(terraform, "HealthCheckPath"),
        "var.eb_health_check_path",
      );

      assert.doesNotMatch(terraform, /value\s+=\s+"5000"/);
      assert.doesNotMatch(terraform, /value\s+=\s+"\/health"/);
    });

    it(`${plugin} preserves Fargate and generic EKS conventions`, () => {
      const terraform = read(plugin, "generate-terraform.md");
      const targetGroup = onlyBlock(
        terraform,
        'resource "aws_lb_target_group" "<app_sanitized>_web" {',
      );
      assert.match(terraform, /containerPort = <port: 8080 for web/);
      assert.match(terraform, /hostPort\s+= <port: 8080 for web/);
      assert.match(terraform, /container_port\s+= 8080/);
      assert.match(targetGroup, /^\s*port\s+= 8080$/m);
      assert.match(targetGroup, /^\s*path\s+= "\/"$/m);

      const eks = read(plugin, "generate-eks.md");
      assert.match(eks, /name: PORT\s*\n\s*value: "8080"/);
      assert.match(eks, /containerPort: 8080/);
      assert.match(eks, /targetPort: 8080/);
    });

    it(`${plugin} explains the required inputs without guessed verification paths`, () => {
      const phase = read(plugin, "generate.md");
      const terraform = read(plugin, "generate-terraform.md");
      const docs = read(plugin, "generate-docs.md");

      assert.doesNotMatch(phase, /deployable artifacts/);
      assert.doesNotMatch(terraform, /deployable Terraform/);
      assert.match(terraform, /terraform plan -input=false/);
      assert.match(docs, /eb_application_port/);
      assert.match(docs, /eb_health_check_path/);
      assert.match(docs, /No value for required variable/);
      assert.match(
        docs,
        /customer-supplied `eb_health_check_path` returns a successful response/,
      );
      assert.match(
        docs,
        /Health check endpoint returns 200: `https:\/\/\{\{ALB_DNS_NAME\}\}\/health`/,
      );
      assert.match(
        docs,
        /Does NOT hard-code a health check path for Elastic Beanstalk verification; use `eb_health_check_path` instead/,
      );
      assert.doesNotMatch(
        docs,
        /Does NOT hard-code a health check path for Elastic Beanstalk or Fargate verification/,
      );
    });
  }

  it("runs provider-free plans with Terraform 1.13", () => {
    const version = runTerraform(process.cwd(), ["version", "-json"]);
    assert.equal(version.status, 0, resultOutput(version));
    assert.match(JSON.parse(version.stdout).terraform_version, /^1\.13\./);
  });

  for (const missing of ["eb_application_port", "eb_health_check_path"]) {
    it(`fails planning when ${missing} is omitted`, () => {
      const directory = terraformFixture(read(PLUGINS[0], "generate-terraform.md"));
      const supplied = missing === "eb_application_port"
        ? "eb_health_check_path=/readyz"
        : "eb_application_port=4321";
      try {
        const plan = runTerraform(directory, [
          "plan",
          "-input=false",
          "-no-color",
          `-var=${supplied}`,
        ]);
        assert.notEqual(plan.status, 0, resultOutput(plan));
        assert.match(resultOutput(plan), /No value for required variable/);
        assert.match(resultOutput(plan), new RegExp(`variable "${missing}"`));
      } finally {
        rmSync(directory, { recursive: true, force: true });
      }
    });
  }

  it("preserves sentinel values in Terraform plan JSON", () => {
    const directory = terraformFixture(read(PLUGINS[0], "generate-terraform.md"));
    try {
      const plan = runTerraform(directory, [
        "plan",
        "-input=false",
        "-no-color",
        "-out=tfplan",
        "-var=eb_application_port=4321",
        "-var=eb_health_check_path=/readyz",
      ]);
      assert.equal(plan.status, 0, resultOutput(plan));

      const show = runTerraform(directory, ["show", "-json", "tfplan"]);
      assert.equal(show.status, 0, resultOutput(show));
      const settings = JSON.parse(show.stdout).planned_values.outputs
        .runtime_settings.value;
      assert.deepEqual(settings, {
        HealthCheckPath: "/readyz",
        PORT: "4321",
      });
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
});
