// application-source-review.ts — production contract checks for a planned
// read-only Heroku application-source review.
//
// Tests import this zero-dependency implementation directly so contract behavior is
// established before any migration phase invokes it.
//
// Later changes add source-path checks, fail-closed retention, command-line
// publication, and workflow activation.

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
