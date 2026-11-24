from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, InstanceInfo, Knowledge, Panel, PassResult, Plugin, ReadError, Setup, Sweep, TestResult, Tool
from skopax.kit import connection
from skopax.plugins.cloudwatch import alarms, logs
from skopax.plugins.cloudwatch.alarms import DEFAULT_MAP, map_problem
from skopax.plugins.cloudwatch.aws import NAME, AwsError, client_for, console_of, region_problem, role_problem
from skopax.plugins.cloudwatch.logs import DEFAULT_MIN, groups_problem, query_problem
from skopax.plugins.cloudwatch.tools import (ALARM_DESCRIPTION, ALARM_SCHEMA, LOGS_DESCRIPTION, LOGS_SCHEMA, METRIC_DESCRIPTION, METRIC_SCHEMA,
                                             cloudwatch_alarm as read_alarm, cloudwatch_logs as read_logs, cloudwatch_metric as read_metric)

PER_RUN = 5
LOG_RUNS = 3
TEST_CALL_S = 7
DOT = ' \u00b7 '
HOSTS = ('monitoring.*.amazonaws.com', 'logs.*.amazonaws.com', 'sts.*.amazonaws.com', 'monitoring.*.amazonaws.com.cn', 'logs.*.amazonaws.com.cn',
         'sts.*.amazonaws.com.cn')
SECTION_HELP = {alarms.SECTION: 'when the alarm check last ran, its ALARM, INSUFFICIENT_DATA and OK counts and caps, and one row per alarm in ALARM or INSUFFICIENT_DATA',
                logs.SECTION: 'when the Logs check last ran and the bytes it scanned, and one row per log group with its error lines of 15 minutes and about an hour'}


def client_of(ctx: Context, timeout: float | None = None):
    return client_for(ctx.instance.config, ctx.http(), *([timeout] if timeout else []))


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        try:
            aws = client_of(ctx, TEST_CALL_S)
            aws.provider.credentials(aws)
            source = aws.provider.source()
            aws.cloudwatch('DescribeAlarms', {'AlarmTypes': alarms.ALARM_TYPES, 'MaxRecords': 1})
            parts = [f'{NAME} {aws.region}', f'credentials: {source}', 'alarms readable']
            entries = logs.wanted(ctx.instance.config)
            if entries:
                aws.logs('DescribeLogGroups', {'logGroupNamePrefix': entries[0].rstrip('*'), 'limit': 1})
                parts.append('log groups readable')
        except AwsError as e:
            raise ReadError(e.code if e.code in (401, 403) else 502 if e.answered else 400, e.reason) from None
        return DOT.join(parts)

    return connection.result(ask)


def failed(check: str, e: AwsError) -> Sweep:
    return Sweep(PassResult(check, False, f'error: {alarms.clean(e.reason, alarms.REASON_LIMIT)}'))


def run_alarms(ctx: Context) -> Sweep:
    try:
        aws = client_of(ctx)
    except AwsError as e:
        return failed(alarms.PASS, e)
    return alarms.sweep(aws, ctx.instance.config, ctx.now, ctx.last_status())


def run_logs(ctx: Context) -> Sweep:
    try:
        aws = client_of(ctx)
    except AwsError as e:
        return failed(logs.PASS, e)
    return logs.sweep(aws, ctx.instance.config, ctx.now, previous=ctx.previous(logs.SECTION))


def aws_of(ctx: Context):
    try:
        return client_of(ctx)
    except AwsError as e:
        raise ReadError(400, e.reason) from None


def cloudwatch_alarm(ctx: Context, args: dict) -> object:
    return read_alarm(aws_of(ctx), args, ctx.now)


def cloudwatch_metric(ctx: Context, args: dict) -> object:
    return read_metric(aws_of(ctx), args, ctx.now)


def cloudwatch_logs(ctx: Context, args: dict) -> object:
    return read_logs(aws_of(ctx), ctx.instance.config, args, ctx.now)


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    region = str(card.get('region') or '')
    if card.get('src') not in (alarms.PASS, logs.PASS) or card.get('sig') not in (alarms.SIG, logs.SIG) or region_problem(region):
        return None
    base = console_of(region)
    if card.get('src') == alarms.PASS:
        return [dict(label='Alarms in CloudWatch', url=base + '#alarmsV2:')]
    return [dict(label='Logs Insights in CloudWatch', url=base + '#logsV2:logs-insights')]


PLUGIN = Plugin(
    setup=Setup(
        steps=(
            'Make an IAM policy with the read-only actions listed in the Skopax docs (cloudwatch:DescribeAlarms, cloudwatch:DescribeAlarmHistory, cloudwatch:GetMetricData, '
            'and the logs: actions if Logs Insights is used).',
            'On EKS: attach the policy to an IAM role whose trust policy accepts the Skopax service account (IRSA), and annotate that service account with '
            'eks.amazonaws.com/role-arn. Leave the access key fields empty.',
            'Elsewhere: attach the policy to an IAM user, make an access key for it, and paste Access key ID and Secret access key.',
            'Set Region, optionally a Role ARN to assume (for another account), and press Test connection.',
            'Logs Insights is billed by the data it scans: leave Log groups empty to keep it off, or name a few small log groups.',
        ),
        docs='https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html',
        docs_label='AWS: IAM roles for service accounts',
    ),
    id='cloudwatch', name=NAME, version='0.1.0', category='alerts',
    description='A card for each AWS CloudWatch alarm in ALARM, optional Logs Insights error counts per log group, and read tools for alarms, metrics and logs.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.5,<0.6', edition='community',
    capabilities=Capabilities(hosts=HOSTS, secrets=('secret-access-key', 'session-token')),
    fields=(Field('region', 'Region', 'text', required=True, check=region_problem, help='The AWS region to read, such as eu-west-1. One instance reads one region.'),
            Field('access-key-id', 'Access key ID', 'text', help='The access key ID of an IAM user with the read-only policy. Leave empty to use IRSA on EKS.'),
            Field('secret-access-key', 'Secret access key', 'secret', help='The secret of that access key. Leave empty to use IRSA on EKS.'),
            Field('session-token', 'Session token', 'secret', help='Only for temporary access keys; they expire, so prefer IRSA or a long-lived key.'),
            Field('role-arn', 'Role ARN', 'text', check=role_problem, help='Optional: an IAM role to assume with sts:AssumeRole, for example one in another account.'),
            Field('alarm-prefixes', 'Alarm name prefixes', 'text', default='', help='Comma-separated alarm name prefixes to watch (at most 10). Empty watches every alarm.'),
            Field('insufficient-data', 'Cards for alarms without data', 'bool', default='false',
                  help='On also opens a Warning card for an alarm in INSUFFICIENT_DATA. Off (the default) opens cards only for alarms in ALARM.'),
            Field('severity-map', 'Severity words', 'text', default=DEFAULT_MAP, check=map_problem,
                  help='word=level pairs, comma-separated: an alarm whose name or description holds the word gets that level (crit, high, warn or info); the highest wins. '
                       '*=level sets the default, which is high.'),
            Field('log-groups', 'Log groups', 'text', default='', check=groups_problem,
                  help='Comma-separated log group names for Logs Insights; end one with * for a prefix. Empty keeps Logs Insights off. It is billed by the data it scans.'),
            Field('error-query', 'Error query', 'text', default='', check=query_problem,
                  help='A Logs Insights query that returns @log and a count n per log group (and optionally sample). Empty uses the built-in error, exception and fatal count.'),
            Field('log-min-lines', 'Lowest error lines', 'int', default=str(DEFAULT_MIN), low=1, high=1000000,
                  help='A log group with fewer matching lines in 15 minutes opens no card.')),
    test=test,
    checks=(Check(alarms.PASS, 'CloudWatch alarms', 900, 275, (alarms.PASS,), run_alarms, tool='cloudwatch_alarm', sections=(alarms.SECTION,),
                  section_help={alarms.SECTION: SECTION_HELP[alarms.SECTION]}),
            Check(logs.PASS, 'CloudWatch Logs errors', 900, 610, (logs.PASS,), run_logs, tool='cloudwatch_logs', sections=(logs.SECTION,),
                  section_help={logs.SECTION: SECTION_HELP[logs.SECTION]})),
    tools=(Tool('cloudwatch_alarm', ALARM_DESCRIPTION, ALARM_SCHEMA, cloudwatch_alarm, per_run=PER_RUN),
           Tool('cloudwatch_metric', METRIC_DESCRIPTION, METRIC_SCHEMA, cloudwatch_metric, per_run=PER_RUN),
           Tool('cloudwatch_logs', LOGS_DESCRIPTION, LOGS_SCHEMA, cloudwatch_logs, per_run=LOG_RUNS)),
    links=links,
    panels=(Panel('cloudwatch', 'panel.js', label='CloudWatch', group='observe'),),
    knowledge=(Knowledge('cloudwatch', NAME, (Path(__file__).parent / 'cloudwatch.md').read_text(encoding='utf-8'), slack=True),),
)
