"""
One-command deploy / update of the three-sleeve PAPER bot (adapted from bear_call_bot/deploy.py).

  python three_sleeve_bot/deploy.py            # create or update everything (idempotent), then INIT (indexes + token seed)
  python three_sleeve_bot/deploy.py --test     # ...then a TEST dry run (Dhan + rules + one Discord message)
  python three_sleeve_bot/deploy.py --remove   # delete schedules, alarms, function and roles (Mongo data is kept)
  python three_sleeve_bot/deploy.py --events-only

Creates / updates in AWS_REGION (default ap-south-1):
  * IAM role for Lambda (CloudWatch Logs only) and a role for EventBridge Scheduler (invoke this function only)
  * Lambda: python3.13, arm64, zip, 256 MB, 240 s; env from the repo .env (secrets live only in Lambda env)
      - async invoke config: 0 retries, max event age 60 s (no duplicate runs after a failure)
      - reserved concurrency 1 (one run at a time)
      - JSON logs; log group retention 14 days
  * EventBridge Scheduler group `three-sleeve`, schedules in Asia/Kolkata, retries 0, stale schedules deleted
  * CloudWatch alarms (Errors >= 1, Throttles >= 5 per 5 min) -> SNS topic -> ALERT_EMAIL (confirm the email once)
  * Shareable console test events from events/*.json
No fallbacks: if AWS denies any step, the deploy stops and names the permission to add (see README).
Needs AWS credentials on this machine (aws configure) and boto3.
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import envfile  # noqa: E402

envfile.load(into_environ=True)
E = os.environ.get
REGION = E("AWS_REGION", "ap-south-1")
FUNCTION = E("LAMBDA_FUNCTION_NAME", "ThreeSleevePaperBot")
ROLE = f"{FUNCTION}Role"
SCHED_ROLE = f"{FUNCTION}SchedulerRole"
GROUP = "three-sleeve"
TOPIC = "three-sleeve-alarms"
RUNTIME_FILES = ["lambda_function.py", "rules.py", "costs.py", "dhan.py", "journal.py", "notify.py",
                 "market_calendar.py", "reconcile.py", "replay.py"]
CONFIG_KEYS = ["DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "MONGODB_URI", "MONGO_DB", "DISCORD_WEBHOOK_URL", "CAPITAL",
               "EXTRA_HOLIDAYS"]
WD = "MON-FRI"
# (name, IST cron, action). The function skips NSE holidays itself; RENEW_TOKEN runs every day.
SCHEDULES = [
    ("RenewToken", "cron(0 6,18 * * ? *)", "RENEW_TOKEN"),
    ("Reconcile", f"cron(15 8 ? * {WD} *)", "RECONCILE"),
    ("Heartbeat", f"cron(45 8 ? * {WD} *)", "HEARTBEAT"),
    ("Snapshot0920", f"cron(21 9 ? * {WD} *)", "SNAPSHOT_0920"),
    ("Entry0931", f"cron(31 9 ? * {WD} *)", "ENTRY_0931"),
    ("Monthly1015", f"cron(15 10 ? * {WD} *)", "MONTHLY_1015"),
    ("TickMorning", f"cron(36,41,46,51,56 9 ? * {WD} *)", "TICK"),
    ("TickDay", f"cron(1/5 10-14 ? * {WD} *)", "TICK"),
    ("TickClose", f"cron(1,6,11,16,21,26,31 15 ? * {WD} *)", "TICK"),
    ("Eod", f"cron(40 15 ? * {WD} *)", "EOD"),
]


def strategy_version():
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        sha = "nogit"
    import rules
    ph = hashlib.sha1(json.dumps(rules.params(), sort_keys=True).encode()).hexdigest()[:8]
    return f"{sha}-{ph}"


def build_zip():
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        print("Installing runtime dependencies for Linux arm64 / Python 3.13 ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt"),
                               "-t", tmp, "--platform", "manylinux2014_aarch64", "--python-version", "3.13",
                               "--implementation", "cp", "--only-binary=:all:", "--upgrade"])
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in RUNTIME_FILES:
                z.write(ROOT / f, f)
            for p in Path(tmp).rglob("*"):
                rel = p.relative_to(tmp)
                if p.is_file() and "__pycache__" not in rel.parts and rel.parts[0] != "bin":
                    z.write(p, rel.as_posix())
    data = buf.getvalue()
    print(f"Package: {len(data) / 1e6:.2f} MB")
    return data


def ensure_role(iam, name, service, policy_name=None, policy=None, managed=None, condition=None):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                                                     "Action": "sts:AssumeRole", **({"Condition": condition} if condition else {})}]}
    created = False
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="three-sleeve paper bot")["Role"]["Arn"]
        created = True
    if managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=managed)
    if policy:
        iam.put_role_policy(RoleName=name, PolicyName=policy_name, PolicyDocument=json.dumps(policy))
    print(f"IAM role {name}: {'created' if created else 'updated'}")
    if created:
        print("  waiting 12 s for IAM propagation ...")
        time.sleep(12)
    return arn


def wait_ready(lam):
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION, WaiterConfig={"Delay": 2, "MaxAttempts": 60})


def deploy_function(lam, logs, role_arn, code):
    env = {k: E(k) for k in CONFIG_KEYS if E(k) not in (None, "")}
    env["STRATEGY_VERSION"] = strategy_version()
    log_group = f"/aws/lambda/{FUNCTION}"
    try:
        logs.create_log_group(logGroupName=log_group)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=log_group, retentionInDays=14)
    cfg = dict(Runtime="python3.13", Role=role_arn, Handler="lambda_function.lambda_handler", Timeout=240,
               MemorySize=256, Environment={"Variables": env}, Description="Three-sleeve PAPER bot (no orders)",
               LoggingConfig={"LogFormat": "JSON", "ApplicationLogLevel": "INFO", "SystemLogLevel": "WARN",
                              "LogGroup": log_group})
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code, Architectures=["arm64"])
        wait_ready(lam)
        lam.update_function_configuration(FunctionName=FUNCTION, **cfg)
        wait_ready(lam)
        print(f"Lambda {FUNCTION}: code and configuration updated ({env['STRATEGY_VERSION']})")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):          # a brand-new role can take a few seconds to be assumable
            try:
                lam.create_function(FunctionName=FUNCTION, Code={"ZipFile": code}, Architectures=["arm64"], **cfg)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 5:
                    raise
                time.sleep(5)
        wait_ready(lam)
        print(f"Lambda {FUNCTION}: created ({env['STRATEGY_VERSION']})")
    lam.put_function_event_invoke_config(FunctionName=FUNCTION, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=60)
    print("  async invocations: 0 retries, max event age 60 s")
    lam.put_function_concurrency(FunctionName=FUNCTION, ReservedConcurrentExecutions=1)
    print("  reserved concurrency: 1")
    return lam.get_function(FunctionName=FUNCTION)["Configuration"]["FunctionArn"]


def deploy_schedules(sch, fn_arn, role_arn):
    try:
        sch.create_schedule_group(Name=GROUP)
        print(f"Scheduler group {GROUP}: created")
    except sch.exceptions.ConflictException:
        pass
    wanted = set()
    for name, cron, action in SCHEDULES:
        wanted.add(name)
        kw = dict(Name=name, GroupName=GROUP, ScheduleExpression=cron, ScheduleExpressionTimezone="Asia/Kolkata",
                  FlexibleTimeWindow={"Mode": "OFF"}, State="ENABLED", Description=f"{action} (IST)",
                  Target={"Arn": fn_arn, "RoleArn": role_arn, "Input": json.dumps({"action": action}),
                          "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 60}})
        try:
            sch.create_schedule(**kw)
            how = "created"
        except sch.exceptions.ConflictException:
            sch.update_schedule(**kw)
            how = "updated"
        print(f"  schedule {name:14s} {cron:40s} {action:14s} {how}")
    for s in sch.list_schedules(GroupName=GROUP, MaxResults=100)["Schedules"]:
        if s["Name"] not in wanted:
            sch.delete_schedule(Name=s["Name"], GroupName=GROUP)
            print(f"  schedule {s['Name']}: deleted (no longer in the list)")


def deploy_alarms(cw, sns):
    email = E("ALERT_EMAIL")
    if not email:
        sys.exit("ALERT_EMAIL is missing in .env (needed for the failure alarm).")
    topic_arn = sns.create_topic(Name=TOPIC)["TopicArn"]
    subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
    if not any(s.get("Endpoint") == email for s in subs):
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
        print(f"  SNS: confirmation email sent to {email} (click the link once)")
    for name, metric, threshold in (("Errors", "Errors", 1), ("Throttles", "Throttles", 5)):
        cw.put_metric_alarm(AlarmName=f"{FUNCTION}-{name}", Namespace="AWS/Lambda", MetricName=metric,
                            Dimensions=[{"Name": "FunctionName", "Value": FUNCTION}], Statistic="Sum", Period=300,
                            EvaluationPeriods=1, Threshold=threshold, ComparisonOperator="GreaterThanOrEqualToThreshold",
                            TreatMissingData="notBreaching", AlarmActions=[topic_arn],
                            AlarmDescription=f"{FUNCTION}: {metric} >= {threshold} in 5 min")
    print(f"  CloudWatch alarms: Errors >= 1, Throttles >= 5 (per 5 min) -> {TOPIC}")


def upload_test_events(session):
    """Console test events are a convenience: this is the one step allowed to be skipped (it changes nothing about how the bot runs)."""
    events = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "events").glob("*.json"))}
    doc = {"openapi": "3.0.0", "info": {"version": "1.0.0", "title": "Event"}, "paths": {},
           "components": {"schemas": {"Event": {"type": "object", "required": ["action"],
                                                "properties": {"action": {"type": "string"}, "date": {"type": "string"},
                                                               "time": {"type": "string"}}}},
                          "examples": {name: {"value": ev} for name, ev in events.items()}}}
    sc = session.client("schemas")
    registry, name = "lambda-testevent-schemas", f"_{FUNCTION}-schema"
    try:
        try:
            sc.describe_registry(RegistryName=registry)
        except sc.exceptions.NotFoundException:
            sc.create_registry(RegistryName=registry, Description="Lambda shareable test events")
        try:
            sc.describe_schema(RegistryName=registry, SchemaName=name)
            sc.update_schema(RegistryName=registry, SchemaName=name, Type="OpenApi3", Content=json.dumps(doc))
        except sc.exceptions.NotFoundException:
            sc.create_schema(RegistryName=registry, SchemaName=name, Type="OpenApi3", Content=json.dumps(doc))
        print(f"Console test events: {', '.join(events)}")
    except ClientError as e:
        print(f"Console test events not published ({e.response['Error']['Code']}); use invoke.py instead")


def invoke(lam, action):
    r = lam.invoke(FunctionName=FUNCTION, InvocationType="RequestResponse",
                   Payload=json.dumps({"action": action, "by": "deploy.py"}).encode())
    body = r["Payload"].read().decode()
    if r.get("FunctionError"):
        sys.exit(f"{action} FAILED: {body[:1500]}")
    print(f"{action}: {body[:1500]}")


def remove_all(session):
    sch, lam, iam, cw = (session.client(x) for x in ("scheduler", "lambda", "iam", "cloudwatch"))
    for s in sch.list_schedules(GroupName=GROUP, MaxResults=100)["Schedules"]:
        sch.delete_schedule(Name=s["Name"], GroupName=GROUP)
    sch.delete_schedule_group(Name=GROUP)
    print("Schedules: deleted")
    cw.delete_alarms(AlarmNames=[f"{FUNCTION}-Errors", f"{FUNCTION}-Throttles"])
    lam.delete_function(FunctionName=FUNCTION)
    print(f"Lambda {FUNCTION}: deleted")
    for role in (ROLE, SCHED_ROLE):
        for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role)
        print(f"IAM role {role}: deleted")
    print("MongoDB data and the SNS topic were kept.")


def deploy(a, session):
    for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "MONGODB_URI", "DISCORD_WEBHOOK_URL", "ALERT_EMAIL"):
        if not E(k):
            sys.exit(f"{k} is missing in .env")
    iam, lam, logs, sch, cw, sns = (session.client(x) for x in ("iam", "lambda", "logs", "scheduler", "cloudwatch", "sns"))
    acct = session.client("sts").get_caller_identity()["Account"]
    print(f"AWS account {acct}, region {REGION}\n")
    role_arn = ensure_role(iam, ROLE, "lambda.amazonaws.com",
                           managed="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    fn_arn = deploy_function(lam, logs, role_arn, build_zip())
    sched_arn = ensure_role(iam, SCHED_ROLE, "scheduler.amazonaws.com", "InvokeBot",
                            {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction",
                                                                     "Resource": [fn_arn, fn_arn + ":*"]}]},
                            condition={"StringEquals": {"aws:SourceAccount": acct}})
    print("EventBridge Scheduler (Asia/Kolkata):")
    deploy_schedules(sch, fn_arn, sched_arn)
    print("Alarms:")
    deploy_alarms(cw, sns)
    upload_test_events(session)
    invoke(lam, "INIT")
    print(f"\nDone. Logs: CloudWatch /aws/lambda/{FUNCTION}")
    if a.test:
        invoke(lam, "TEST")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--events-only", action="store_true")
    a = ap.parse_args()
    session = boto3.Session(region_name=REGION)
    if session.get_credentials() is None:
        sys.exit("No AWS credentials found. Run `aws configure`.")
    try:
        if a.remove:
            remove_all(session)
        elif a.events_only:
            upload_test_events(session)
        else:
            deploy(a, session)
    except ClientError as e:
        err = e.response["Error"]
        if err.get("Code") in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthorizationError"):
            sys.exit(f"\nSTOPPED: AWS denied {e.operation_name}.\n{err.get('Message', '')}\n"
                     "Add that permission to your IAM user (README: 'AWS permissions') and run deploy.py again.")
        raise


if __name__ == "__main__":
    main()
