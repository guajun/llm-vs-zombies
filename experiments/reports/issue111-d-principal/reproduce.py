import json
from pathlib import Path
from llm_vs_zombies import records, audit_compare, action_compare, engine_replay, lifecycle_compare, lifecycle_report
root=Path.cwd(); out=root/'work/principal-acceptance';out.mkdir(exist_ok=True)
arms=['off-a','on-a','off-b','on-b']; runs={a:root/'experiments/runs'/f'issue111-d-hosted-v2-{a}-s42-c0' for a in arms}
audits={};traces={};summary={'archives':{},'comparisons':{},'first_kill':{}}
for a,p in runs.items():
 summary['archives'][a]=records.validate(p)
 audits[a]=audit_compare.AuditLog(p/'audit',require_closed=True)
 traces[a]=engine_replay._trace_steps(p/'decisions/evaluation.jsonl')
 engine_replay._validate_steps(*traces[a],audits[a])
 print('validated',a,flush=True)
for a,b in [('off-a','on-a'),('off-a','off-b'),('on-a','on-b')]:
 common=audit_compare.compare_common_audits(audits[a],audits[b]); actions=action_compare.compare_action_steps(traces[a][1],traces[b][1]);outcome=audit_compare.first_difference(action_compare._outcome(runs[a],traces[a][0]),action_compare._outcome(runs[b],traces[b][0]))
 report={'common':common,'actions':actions,'outcome_difference':outcome}
 if a[:3]==b[:3]: report['lifecycle']=lifecycle_compare.compare_lifecycle(runs[a]/'audit',runs[b]/'audit')
 report['equal']=common['equal'] and actions['equal'] and not outcome and report.get('lifecycle',{}).get('equal',True)
 summary['comparisons'][a+'-'+b]=report
 print('compared',a,b,report['equal'],flush=True)
for a in ['on-a','on-b']:
 report=lifecycle_report.report_for_run(runs[a],plan=root/'experiments/plans/issue99-shovel-control.json')
 (out/f'{a}-first-kill.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
 summary['first_kill'][a]=report['first_kill'];print('first_kill',a,report['first_kill']['proven'],flush=True)
summary['ok']=all(r['equal'] for r in summary['comparisons'].values()) and all(r['proven'] for r in summary['first_kill'].values())
(out/'acceptance.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
print('FINAL',summary['ok'],flush=True)
