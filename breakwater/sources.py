from __future__ import annotations


JIRA_ANALYZE_SOURCE = "jira_analyze"
JIRA_ISSUE_AUTO_ANALYZE_SOURCE = "jira_issue_auto_analyze"
JIRA_STATUS_SUMMARY_SOURCE = "jira_status_summary"
LARK_JIRA_ANALYZE_SOURCE = "lark_jira_analyze"
GITHUB_ISSUE_ANALYZE_SOURCE = "github_issue_analyze"

DIRECT_JIRA_ANALYSIS_SOURCES = frozenset(
    {JIRA_ANALYZE_SOURCE, JIRA_ISSUE_AUTO_ANALYZE_SOURCE, JIRA_STATUS_SUMMARY_SOURCE}
)
JIRA_AUTOMATION_REPORT_SOURCES = frozenset({JIRA_ISSUE_AUTO_ANALYZE_SOURCE, JIRA_STATUS_SUMMARY_SOURCE})


def is_direct_jira_analysis_source(source: str | None) -> bool:
    return source in DIRECT_JIRA_ANALYSIS_SOURCES


def requires_jira_automation_report(source: str | None) -> bool:
    return source in JIRA_AUTOMATION_REPORT_SOURCES
