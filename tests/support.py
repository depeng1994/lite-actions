"""Synthetic channel fixtures for retained low-level execution regressions."""
import json

def load_config(root):
    pools=json.loads((root/"config/pools.json").read_text())
    defs={}
    for name,nodes in [("a3-8p",1),("a3-16p",2),("a5-64p",8)]:
        defs[name]=dict(name=name,pipeline_id=name,workflow=name.replace("-","_")+"_lite_actions.yml",pool="a5_shared_yellow" if nodes==8 else "a3_shared_blue",runner={"kind":"ssh"},enabled=nodes!=8,resources=dict(nodes=nodes,devices_per_node=8),timeout_seconds=7500)
    defs["dispatcher-smoke"]=dict(name="dispatcher-smoke",pipeline_id="npu-smi",workflow="lite_actions_dispatcher_smoke.yml",pool="local-smoke",allocation=[],runner={"kind":"worker","worker_id":"depeng"},enabled=True,timeout_seconds=90)
    return defs
