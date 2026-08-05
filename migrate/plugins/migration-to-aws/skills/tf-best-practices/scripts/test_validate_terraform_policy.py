"""Tests for validate-terraform-policy.py (tf-best-practices, read-only verdict)."""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404 — test-only, inputs are hardcoded literals
import sys
import tempfile
from pathlib import Path

PLUGIN_SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_SKILL_ROOT / "scripts" / "validate-terraform-policy.py"
FIXTURES = PLUGIN_SKILL_ROOT / "fixtures" / "terraform-policy"
GOOD_FIXTURE = FIXTURES / "good-https-redirect"
BAD_HTTP_FORWARD = FIXTURES / "bad-http-forward"
INTERNAL_ALB = FIXTURES / "internal-alb-only"
BAD_RDS_PUBLIC_UNENCRYPTED = FIXTURES / "bad-rds-public-unencrypted"
GOOD_RDS_PRIVATE_ENCRYPTED = FIXTURES / "good-rds-private-encrypted"
BAD_DB_SG_PUBLIC = FIXTURES / "bad-db-sg-public"
GOOD_DB_SG_SCOPED = FIXTURES / "good-db-sg-scoped"
BAD_WILDCARD_IAM = FIXTURES / "bad-wildcard-iam"
GOOD_SCOPED_IAM = FIXTURES / "good-scoped-iam"
BAD_WILDCARD_IAM_LISTFORM = FIXTURES / "bad-wildcard-iam-listform"
GOOD_IAM_SCOPED_LIST = FIXTURES / "good-iam-scoped-list"
GOOD_INTERNET_NLB = FIXTURES / "good-internet-nlb"
BAD_SG_PUBLIC_SSH = FIXTURES / "bad-sg-public-ssh"
GOOD_SG_PUBLIC_WEBAPP = FIXTURES / "good-sg-public-webapp"
BAD_ELASTICACHE_UNENCRYPTED = FIXTURES / "bad-elasticache-unencrypted"
GOOD_ELASTICACHE_ENCRYPTED = FIXTURES / "good-elasticache-encrypted"
GOOD_QUOTED_PORT_HTTPS = FIXTURES / "good-quoted-port-https"
BAD_DB_SG_PUBLIC_QUOTED_PORT = FIXTURES / "bad-db-sg-public-quoted-port"


def _load_validator_module():
    """Import the validator for unit-level tests (its filename is hyphenated, so
    it is not importable as a normal module name)."""
    spec = importlib.util.spec_from_file_location("validate_terraform_policy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves annotations via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator_module()


def run_policy_validator(terraform_dir: Path, json_out: Path | None = None) -> tuple[int, str]:
    cmd = [sys.executable, str(SCRIPT), str(terraform_dir)]
    if json_out is not None:
        cmd += ["--json", str(json_out)]
    result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
    return result.returncode, result.stdout + result.stderr


def test_good_https_redirect_passes() -> None:
    code, out = run_policy_validator(GOOD_FIXTURE)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_bad_http_forward_fails() -> None:
    code, out = run_policy_validator(BAD_HTTP_FORWARD)
    assert code == 1, out
    assert "POLICY_FAIL" in out
    assert "redirect" in out.lower()


def test_internal_alb_skips_https_requirement() -> None:
    code, out = run_policy_validator(INTERNAL_ALB)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_json_report_written_on_pass() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "verdict.json"
        code, _ = run_policy_validator(GOOD_FIXTURE, json_out=out_path)
        assert code == 0
        report = json.loads(out_path.read_text())
        assert report["policy_status"] == "POLICY_OK"
        assert report["violations"] == []


def test_json_report_written_on_fail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "verdict.json"
        code, _ = run_policy_validator(BAD_HTTP_FORWARD, json_out=out_path)
        assert code == 1
        report = json.loads(out_path.read_text())
        assert report["policy_status"] == "POLICY_FAIL"
        rules = {v["rule"] for v in report["violations"]}
        assert "alb_http_redirect" in rules


def test_missing_directory_is_usage_error() -> None:
    code, out = run_policy_validator(Path("/nonexistent/terraform/dir"))
    assert code == 2, out
    assert "not_a_directory" in out


def test_no_tf_files_flagged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        code, out = run_policy_validator(Path(tmp))
        assert code == 1, out
        assert "No .tf files" in out


def test_rds_public_and_unencrypted_fails() -> None:
    code, out = run_policy_validator(BAD_RDS_PUBLIC_UNENCRYPTED)
    assert code == 1, out
    assert "POLICY_FAIL" in out


def test_rds_public_unencrypted_reports_both_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "verdict.json"
        run_policy_validator(BAD_RDS_PUBLIC_UNENCRYPTED, json_out=out_path)
        rules = {v["rule"] for v in json.loads(out_path.read_text())["violations"]}
        assert "rds_not_public" in rules
        assert "rds_encryption_at_rest" in rules


def test_rds_private_encrypted_passes() -> None:
    # Also proves variable-driven storage_encrypted fails open (not flagged).
    code, out = run_policy_validator(GOOD_RDS_PRIVATE_ENCRYPTED)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_db_sg_public_ingress_fails() -> None:
    code, out = run_policy_validator(BAD_DB_SG_PUBLIC)
    assert code == 1, out
    assert "db_sg_no_public_ingress" in out


def test_db_sg_scoped_passes() -> None:
    # Proves: app-SG-scoped DB ingress OK; public 443 not flagged as a DB port;
    # separate aws_vpc_security_group_ingress_rule fails open (not correlated).
    code, out = run_policy_validator(GOOD_DB_SG_SCOPED)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_wildcard_iam_fails() -> None:
    code, out = run_policy_validator(BAD_WILDCARD_IAM)
    assert code == 1, out
    assert "no_wildcard_iam" in out


def test_scoped_iam_passes() -> None:
    # Proves scoped policy OK and assume-role trust policy is not a wildcard hit.
    code, out = run_policy_validator(GOOD_SCOPED_IAM)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_wildcard_iam_listform_fails() -> None:
    # Regression: Resource = ["*"] (single-element list) must fail, like "*".
    code, out = run_policy_validator(BAD_WILDCARD_IAM_LISTFORM)
    assert code == 1, out
    assert "no_wildcard_iam" in out


def test_iam_scoped_list_passes() -> None:
    # A list of scoped actions / resource ARNs is not a wildcard — must pass.
    code, out = run_policy_validator(GOOD_IAM_SCOPED_LIST)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_internet_facing_nlb_not_flagged() -> None:
    # Regression: an internet-facing Network LB (L4) has no HTTPS:443 listener
    # by design and must NOT trip the ALB HTTPS rule.
    code, out = run_policy_validator(GOOD_INTERNET_NLB)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_sg_public_admin_ingress_fails() -> None:
    code, out = run_policy_validator(BAD_SG_PUBLIC_SSH)
    assert code == 1, out
    assert "sg_no_public_admin_ingress" in out


def test_sg_public_admin_ingress_reports_ssh_and_redis() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "verdict.json"
        run_policy_validator(BAD_SG_PUBLIC_SSH, json_out=out_path)
        summaries = " ".join(
            v["summary"] for v in json.loads(out_path.read_text())["violations"]
        )
        assert "22 (SSH)" in summaries
        assert "6379 (Redis)" in summaries


def test_sg_public_webapp_passes() -> None:
    # Public web ports (80/443), a high game-port range, and a privately-scoped
    # SSH rule must all pass — no false positive from the sensitive-port rule.
    code, out = run_policy_validator(GOOD_SG_PUBLIC_WEBAPP)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_elasticache_unencrypted_fails() -> None:
    code, out = run_policy_validator(BAD_ELASTICACHE_UNENCRYPTED)
    assert code == 1, out
    assert "elasticache_encryption_at_rest" in out


def test_elasticache_encrypted_passes() -> None:
    # Encrypted RG passes; variable-driven RG fails open (not flagged).
    code, out = run_policy_validator(GOOD_ELASTICACHE_ENCRYPTED)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_block_form_forward_https_listener_does_not_false_fail() -> None:
    """Regression: a valid HTTPS listener whose default_action uses a NESTED
    forward { ... } block before `type` must NOT be misparsed as missing/wrong.

    The old r'default_action{[^}]*?type' regex stopped at the first '}' (the end
    of the nested block) and failed to read `type`, producing a false POLICY_FAIL.
    """
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "compute.tf").write_text(
            """
resource "aws_lb" "app" {
  name     = "app-alb"
  internal = false
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  # Placeholder — the checker only tests for the presence of `certificate_arn`,
  # not its value (see has_certificate_arn). A literal ACM ARN here trips secret
  # scanners for no benefit, so use a variable reference.
  certificate_arn   = var.acm_certificate_arn

  default_action {
    forward {
      target_group {
        arn = aws_lb_target_group.app.arn
      }
    }
    type = "forward"
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
""",
            encoding="utf-8",
        )
        code, out = run_policy_validator(Path(tmp))
        assert code == 0, f"block-form forward should pass, got: {out}"
        assert "POLICY_OK" in out


def test_quoted_port_https_listener_passes() -> None:
    """Regression: Terraform accepts a quoted integer for number arguments, so a
    correct HTTPS listener written as port = "443" must NOT be falsely flagged.

    Reading only bare integers left port=None, so https_ok was empty and a valid
    stack failed with rule=alb_https_listener.
    """
    code, out = run_policy_validator(GOOD_QUOTED_PORT_HTTPS)
    assert code == 0, out
    assert "POLICY_OK" in out


def test_quoted_port_db_sg_public_ingress_fails() -> None:
    """Regression: from_port / to_port = "5432" with cidr_blocks = ["0.0.0.0/0"]
    is a world-open database and must fire, not silently pass."""
    code, out = run_policy_validator(BAD_DB_SG_PUBLIC_QUOTED_PORT)
    assert code == 1, out
    assert "db_sg_no_public_ingress" in out


def test_quoted_port_sg_public_admin_ingress_fails() -> None:
    """Same quoted-port regression for the sensitive-port rule (SSH on 22)."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "vpc.tf").write_text(
            """
resource "aws_security_group" "bad" {
  name = "bad-ssh-sg"

  ingress {
    from_port   = "22"
    to_port     = "22"
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
""",
            encoding="utf-8",
        )
        code, out = run_policy_validator(Path(tmp))
        assert code == 1, out
        assert "sg_no_public_admin_ingress" in out
        assert "22 (SSH)" in out


def test_attr_int_reads_bare_and_quoted_integers() -> None:
    assert VALIDATOR._attr_int("  port = 443\n", "port") == 443
    assert VALIDATOR._attr_int('  port = "443"\n', "port") == 443
    assert VALIDATOR._attr_int('  from_port = "0"\n', "from_port") == 0


def test_attr_int_rejects_non_literal_quoted_values() -> None:
    """Precision guard: a quoted value must be ENTIRELY digits. Anything else is
    non-literal and must return None so the rules keep failing open."""
    for body in (
        '  port = "443x"\n',
        '  port = "x443"\n',
        '  port = "443-444"\n',
        '  port = "${var.port}"\n',
        '  port = " 443"\n',
        '  port = ""\n',
        "  port = var.port\n",
        "  port = local.ports[0]\n",
    ):
        assert VALIDATOR._attr_int(body, "port") is None, body


def test_attr_int_absent_attribute_is_none() -> None:
    assert VALIDATOR._attr_int('  protocol = "HTTPS"\n', "port") is None


# A listener whose own `port`/`protocol` are written AFTER its default_action.
# Valid HCL — `terraform fmt` does not reorder attributes relative to blocks — and
# the nested redirect carries both a `port` and a `protocol`, so a whole-body read
# returns the redirect's 443/HTTPS instead of the listener's own 80/HTTP.
_LISTENER_ATTRS_AFTER_NESTED_BLOCK = """{
  load_balancer_arn = aws_lb.app.arn

  default_action {
    type = "redirect"
    redirect {
      port     = "443"
      protocol = "HTTPS"
    }
  }

  port     = 80
  protocol = "HTTP"
}"""


def test_attrs_read_from_own_block_not_nested_block() -> None:
    """Accepting quoted integers makes a nested `port = "443"` a match candidate
    where a bare-only pattern could never match one, so reads are scoped to the
    block's own attributes. Pinned for both readers: _attr_string had the same
    whole-body weakness before this change and shares the scoping now."""
    assert VALIDATOR._attr_int(_LISTENER_ATTRS_AFTER_NESTED_BLOCK, "port") == 80
    assert (
        VALIDATOR._attr_string(_LISTENER_ATTRS_AFTER_NESTED_BLOCK, "protocol") == "HTTP"
    )


def test_attr_nested_only_value_is_not_promoted() -> None:
    """An attribute that exists ONLY inside a nested block is absent from the
    enclosing block — returning the nested value would invent an attribute.

    Written as `terraform fmt` emits it (one attribute per line), which is the form
    that actually reaches the line-anchored pattern; a collapsed one-line
    `redirect { port = "443" }` never matches and so would not exercise scoping."""
    body = '{\n  default_action {\n    redirect {\n      port = "443"\n    }\n  }\n}'
    assert VALIDATOR._attr_int(body, "port") is None


def test_attr_interpolation_does_not_hide_later_attributes() -> None:
    """`${...}` braces are balanced, so an interpolated value earlier in the body
    must not make a following top-level attribute unreadable."""
    body = '{\n  identifier        = "${var.project}-db"\n  storage_encrypted = true\n}'
    assert VALIDATOR._attr_string(body, "storage_encrypted") == "true"


def test_lexically_irrelevant_braces_do_not_hide_attributes() -> None:
    """A brace in a comment, string, or heredoc is not a block delimiter, so it must
    not shift a real top-level attribute out of scope.

    Regression guard: an earlier revision counted raw `{`/`}` and DID hide the
    attribute here. For these rules a hidden attribute means a MISSED violation
    (`publicly_accessible = true` read as absent, hence compliant), so "fail open"
    is the wrong default — the value is a plain top-level literal, not ambiguous."""
    for label, body in (
        ("hash comment", '{\n  # primary { instance\n  port = 80\n}'),
        ("slash comment", '{\n  // primary { instance\n  port = 80\n}'),
        ("block comment", '{\n  /* primary { instance */\n  port = 80\n}'),
        ("string", '{\n  identifier = "primary { db"\n  port = 80\n}'),
        ("unbalanced string", '{\n  name = "a { b"\n  port = 80\n}'),
        ("heredoc", '{\n  policy = <<EOT\n  { not code }\nEOT\n  port = 80\n}'),
    ):
        assert VALIDATOR._attr_int(body, "port") == 80, label


def test_commented_out_attribute_is_not_read() -> None:
    """An attribute that only appears inside a comment is not set."""
    assert VALIDATOR._attr_int("{\n  # port = 443\n}", "port") is None
    assert VALIDATOR._attr_int("{\n  // port = 443\n}", "port") is None
    assert VALIDATOR._attr_int("{\n  /*\n  port = 443\n  */\n}", "port") is None
    assert (
        VALIDATOR._attr_string('{\n  /*\n  protocol = "HTTPS"\n  */\n}', "protocol")
        is None
    )


def test_every_fixture_matches_its_good_bad_prefix() -> None:
    """Enforce the naming invariant the fixture set relies on: `good-*` (and the
    internal-ALB case) must be POLICY_OK, `bad-*` must be POLICY_FAIL. Without
    this, each fixture is wired up by hand and a new one added without its own
    test would silently get no coverage."""
    fixture_dirs = sorted(d for d in FIXTURES.iterdir() if d.is_dir())
    assert len(fixture_dirs) >= 18, f"unexpectedly few fixtures: {fixture_dirs}"
    for fixture in fixture_dirs:
        code, out = run_policy_validator(fixture)
        if fixture.name.startswith("bad-"):
            assert code == 1, f"{fixture.name} must FAIL: {out}"
            assert "POLICY_FAIL" in out, f"{fixture.name}: {out}"
        else:
            assert code == 0, f"{fixture.name} must pass: {out}"
            assert "POLICY_OK" in out, f"{fixture.name}: {out}"
