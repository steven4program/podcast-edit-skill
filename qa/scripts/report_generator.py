#!/usr/bin/env python3
"""
Layer 3: combined QA report generator.

Merges Layer 1 (signal analysis) and Layer 2 (AI listening evaluation) results
into a structured JSON report plus a human-readable Markdown summary.

Usage:
    python3 report_generator.py --signal qa_signal_report.json --output qa_report.json --summary qa_summary.md
    python3 report_generator.py --signal qa_signal_report.json --ai qa_ai_report.json --output qa_report.json --summary qa_summary.md
"""

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


def format_time(seconds):
    """Format seconds as MM:SS or H:MM:SS."""
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def recalculate_signal_score(signal_report, podcast_mode=True):
    """
    Re-score the signal report (podcast mode applies looser thresholds).

    Natural intonation shifts in podcasts can trip the low-threshold energy
    checks. AI re-review confirms that energy_jump in podcasts is almost
    always a false positive (natural speaker swap or intonation change) —
    even a 105x energy ratio is normal speaker switching.

    In podcast mode:
    - energy_jump is fully ignored (AI has confirmed it is all false positives)
    - keep spectral_jump (could indicate background-noise change)
    - keep unnatural_silence (could indicate cut artifacts)
    - ZCR and breath-truncation produce too many false positives — ignore
    """
    issues = signal_report.get('issues', [])

    if podcast_mode:
        significant = []
        for issue in issues:
            if issue['type'] == 'spectral_jump':
                significant.append(issue)
            elif issue['type'] == 'unnatural_silence':
                significant.append(issue)
            # energy_jump: all false positives in podcasts — ignore
            # zcr_discontinuity: too noisy in podcasts — ignore
            # breath_truncation: too noisy in podcasts — ignore
    else:
        significant = issues

    high = sum(1 for i in significant if i.get('severity') == 'high')
    medium = sum(1 for i in significant if i.get('severity') == 'medium')
    low = sum(1 for i in significant if i.get('severity') == 'low')

    deduction = high * 0.8 + medium * 0.3 + low * 0.1
    score = max(1.0, round(10.0 - deduction, 1))

    return score, significant


def merge_scores(signal_score, ai_score=None):
    """Merge the two layer scores."""
    if ai_score is not None:
        # AI listening is weighted higher (human-ear judgment is more reliable)
        return round(0.4 * signal_score + 0.6 * ai_score, 1)
    return signal_score


def collect_review_items(signal_issues, ai_evals=None):
    """Collect segments that require manual listening."""
    items = []

    # From signal analysis (already-filtered significant issues)
    for issue in signal_issues:
        items.append({
            "time": issue['timestamp'],
            "time_str": format_time(issue['timestamp']),
            "source": "signal",
            "type": issue['type'],
            "severity": issue['severity'],
            "detail": issue['detail'],
            "suggestion": issue.get('suggestion', ''),
            "listen_range": issue.get('listen_range', []),
        })

    # From AI evaluation
    if ai_evals:
        for ev in ai_evals:
            if ev['strategy'] == 'global_sampling':
                for issue in ev.get('issues', []):
                    items.append({
                        "time": issue.get('time', 0),
                        "time_str": format_time(issue.get('time', 0)),
                        "source": "ai",
                        "type": "ai_detected",
                        "severity": "medium",
                        "detail": issue.get('description', ''),
                        "suggestion": "人工複聽確認",
                        "listen_range": ev.get('clip_range', []),
                    })
            elif ev['strategy'] == 'suspicious_review' and ev.get('is_real_issue'):
                orig = ev.get('original_issue', {})
                items.append({
                    "time": orig.get('timestamp', 0),
                    "time_str": format_time(orig.get('timestamp', 0)),
                    "source": "ai_confirmed",
                    "type": orig.get('type', 'unknown'),
                    "severity": "high",
                    "detail": f"AI 確認：{ev.get('explanation', '')}",
                    "suggestion": ev.get('explanation', ''),
                    "listen_range": ev.get('clip_range', []),
                })

    # Sort by time and dedupe (merge same-source items within 5s)
    items.sort(key=lambda x: x['time'])
    deduped = []
    for item in items:
        if not deduped or abs(item['time'] - deduped[-1]['time']) > 5 or item['source'] != deduped[-1]['source']:
            deduped.append(item)

    return deduped


def generate_markdown(report):
    """Generate a human-readable Markdown summary."""
    lines = []
    lines.append("# 播客剪輯品質檢測報告\n")
    lines.append(f"**音訊**：{report['audio_file']}")
    lines.append(f"**長度**：{format_time(report['duration_seconds'])}")

    cut_points = report.get('detected_cut_points', 'N/A')
    lines.append(f"**偵測剪切點**：{cut_points} 個")

    score = report['overall_score']
    score_emoji = "🟢" if score >= 8 else "🟡" if score >= 6 else "🔴"
    lines.append(f"**總體評分**：{score_emoji} {score} / 10\n")

    # Score breakdown
    if report.get('ai_score') is not None:
        lines.append(f"> 信號評分：{report['signal_score']}/10 | AI 評分：{report['ai_score']}/10 | 綜合：{score}/10\n")
    else:
        lines.append(f"> 信號評分：{report['signal_score']}/10（未使用 AI 評估）\n")

    # Items needing manual review
    review_items = report.get('review_items', [])
    if review_items:
        lines.append(f"## 需人工複聽片段（{len(review_items)} 個）\n")
        lines.append("| # | 時間 | 來源 | 問題類型 | 嚴重度 | 說明 |")
        lines.append("|---|------|------|----------|--------|------|")
        for i, item in enumerate(review_items, 1):
            source_label = {"signal": "信號", "ai": "AI", "ai_confirmed": "AI 確認"}.get(item['source'], item['source'])
            sev_label = {"high": "🔴 HIGH", "medium": "🟡 MED", "low": "🟢 LOW"}.get(item['severity'], item['severity'])
            detail = item['detail'][:60] + "..." if len(item['detail']) > 60 else item['detail']
            lines.append(f"| {i} | {item['time_str']} | {source_label} | {item['type']} | {sev_label} | {detail} |")

        # Estimate review time
        total_listen = len(review_items) * 5  # roughly 5s per point
        lines.append(f"\n> 只需複聽以上 {len(review_items)} 個片段（約 {total_listen} 秒），無需聽完整集。\n")
    else:
        lines.append("## ✅ 無需人工複聽\n")
        lines.append("所有偵測點均通過，音訊品質良好。\n")

    # AI false-positive analysis (if any)
    if report.get('ai_summary'):
        ai = report['ai_summary']
        if ai.get('false_positives', 0) > 0:
            total_checked = ai.get('suspicious_clips', 0)
            fp = ai['false_positives']
            lines.append(f"## AI 複查結果\n")
            lines.append(f"- 複查 Layer 1 的 {total_checked} 個 HIGH 問題")
            lines.append(f"- ✅ 誤報：{fp} 個（{fp/max(total_checked,1)*100:.0f}%）")
            lines.append(f"- ⚠️ 確認：{ai.get('confirmed_issues', 0)} 個\n")

    # Stats
    lines.append("## 統計\n")
    stats = report.get('signal_summary', {})
    lines.append(f"- 原始偵測問題：{stats.get('original_total', 'N/A')} 個")
    lines.append(f"- 播客模式過濾後：{stats.get('filtered_total', 'N/A')} 個")
    if review_items:
        high = sum(1 for r in review_items if r['severity'] == 'high')
        med = sum(1 for r in review_items if r['severity'] == 'medium')
        low = sum(1 for r in review_items if r['severity'] == 'low')
        lines.append(f"- HIGH: {high} | MEDIUM: {med} | LOW: {low}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Podcast QA report generator (Layer 3)")
    parser.add_argument("--signal", "-s", required=True, help="Layer 1 signal report JSON")
    parser.add_argument("--ai", "-a", help="Layer 2 AI report JSON (optional)")
    parser.add_argument("--output", "-o", required=True, help="Output combined report JSON")
    parser.add_argument("--summary", help="Output Markdown summary path")
    args = parser.parse_args()

    # Load Layer 1
    if not Path(args.signal).exists():
        print(f"❌ 找不到信號報告：{args.signal}")
        sys.exit(1)

    with open(args.signal) as f:
        signal_report = json.load(f)

    print(f"📋 Layer 1：{signal_report.get('summary', {}).get('total_issues', 0)} 個原始問題")

    # Recompute under podcast mode
    signal_score, significant_issues = recalculate_signal_score(signal_report, podcast_mode=True)
    print(f"   播客模式過濾後：{len(significant_issues)} 個顯著問題")
    print(f"   信號評分：{signal_score}/10")

    # Load Layer 2 if provided
    ai_report = None
    ai_score = None
    ai_evals = None
    if args.ai and Path(args.ai).exists():
        with open(args.ai) as f:
            ai_report = json.load(f)
        ai_score = ai_report.get('ai_score')
        ai_evals = ai_report.get('evaluations', [])
        print(f"📋 Layer 2：AI 評分 {ai_score}/10")

    # Merge scores
    overall = merge_scores(signal_score, ai_score)
    print(f"\n📊 綜合評分：{overall}/10")

    # Collect review items
    review_items = collect_review_items(significant_issues, ai_evals)
    print(f"   需複聽：{len(review_items)} 個片段")

    # Build combined report
    report = {
        "audio_file": signal_report.get('audio_file', ''),
        "duration_seconds": signal_report.get('duration_seconds', 0),
        "detected_cut_points": signal_report.get('detected_cut_points', 0),
        "signal_score": signal_score,
        "ai_score": ai_score,
        "overall_score": overall,
        "review_items": review_items,
        "signal_summary": {
            "original_total": signal_report.get('summary', {}).get('total_issues', 0),
            "filtered_total": len(significant_issues),
        },
        "ai_summary": ai_report.get('summary') if ai_report else None,
    }

    # Save JSON
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 報告已儲存：{args.output}")

    # Generate Markdown
    if args.summary:
        md = generate_markdown(report)
        with open(args.summary, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f"✅ 摘要已儲存：{args.summary}")
        print(f"\n{'='*50}")
        print(md)


if __name__ == "__main__":
    main()
