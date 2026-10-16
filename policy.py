"""核验前分流。没有标签、分数、Token 或小组依赖；不自动批准任何结论。"""
from datetime import datetime
import math


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return parsed


def lag_minutes(check_time, doc_synced_at):
    lag = (parse_time(check_time) - parse_time(doc_synced_at)).total_seconds() / 60
    if lag < 0:
        raise ValueError("同步时间晚于核验时间")
    return lag


def route(check_time, doc_synced_at, threshold_minutes=15, team_code=None, eligible_teams=None):
    if not math.isfinite(threshold_minutes) or threshold_minutes < 0:
        raise ValueError("阈值必须为非负有限值")
    try:
        lag = lag_minutes(check_time, doc_synced_at)
    except (ValueError, TypeError, AttributeError):
        return {"action": "REVIEW_TIME_ERROR", "lag_minutes": None,
                "reason": "时间缺失、无时区、格式错误或出现未来同步时间；人工核对"}
    if eligible_teams is not None:
        if not team_code:
            return {"action": "REVIEW_TEAM_ERROR", "lag_minutes": lag, "reason": "缺少小组，人工确认适用范围"}
        if team_code not in eligible_teams:
            return {"action": "CONTINUE_CHECK", "lag_minutes": lag,
                    "reason": "该小组未纳入同步试点，继续原审查流程；非自动合格"}
    return {
        "action": "WAIT_SYNC" if lag > threshold_minutes else "CONTINUE_CHECK",
        "lag_minutes": lag,
        "reason": f"底稿同步间隔超过 {threshold_minutes:g} 分钟，核对版本并同步后重新核验"
        if lag > threshold_minutes else "继续原核验与律师审查流程，非自动合格",
    }
