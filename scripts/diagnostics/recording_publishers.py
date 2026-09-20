#!/usr/bin/env python3
"""Hardware-free regression using actual recording publishers and Recorder."""
import argparse
import json
from pathlib import Path
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--topic',choices=['infer','authority'],required=True)
    p.add_argument('--legacy',action='store_true')
    p.add_argument('--episodes',type=int,default=20)
    p.add_argument('--messages',type=int,default=400)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    from arx5_collection.infer.recording import RosCommandPublisher, CommandRecord
    from arx5_collection.dagger.authority_ros import RosAuthorityEventPublisher
    from arx5_collection.dagger.takeover import AuthorityEvent, AuthorityEventType
    from arx5_collection.ros2_adapters.recording import RosbagRecordingBackend
    from arx5_collection.ros2_adapters.mcap_metrics import _open_reader
    from arx5_collection.episode.models import StreamSpec
    from arx5_collection_interfaces import msg
    from rclpy.serialization import deserialize_message
    topic='/infer/command' if a.topic=='infer' else '/dagger/authority'
    message_type=msg.InferCommand if a.topic=='infer' else msg.AuthorityEvent
    cls=RosCommandPublisher if a.topic=='infer' else RosAuthorityEventPublisher
    a.output.mkdir(parents=True,exist_ok=False)
    (a.output/'config.json').write_text(json.dumps(dict(vars(a),output=str(a.output),rate_hz=50,hardware=False,model=False),indent=2))
    summaries=[]
    with cls() as publisher:
        node=publisher._node; context=publisher._context
        backend=RosbagRecordingBackend()
        if not a.legacy:
            backend.recording_publisher=publisher
        for ep in range(a.episodes):
            ep_id=f'{a.topic}-{ep+1:03d}'
            directory=a.output/ep_id; directory.mkdir()
            sent=[]; ticks={}
            ticks['start_begin']=time.monotonic_ns()
            backend.start(directory/'episode.mcap',(StreamSpec('record',topic,True,50),))
            ticks['recorder_ready']=time.monotonic_ns()
            if not a.legacy:
                assert publisher._node is node and publisher._context is context
                assert publisher._publisher is not None
            time.sleep(.2)
            started=time.monotonic_ns()
            try:
                for step in range(a.messages):
                    remain=started+step*20_000_000-time.monotonic_ns()
                    if remain>0: time.sleep(remain/1e9)
                    mono=time.monotonic_ns()
                    if a.topic=='infer':
                        publisher(CommandRecord(ep_id,step,mono,publisher.now_ns(),tuple([step/1000.]*14)))
                        seq=step
                    else:
                        seq=ep*a.messages+step+1
                        publisher(AuthorityEvent(seq,mono,step,ep,AuthorityEventType.HUMAN_ACTIVE,f'{ep_id}:{step}'))
                    sent.append({'step':step,'sequence':seq,'mono':mono})
                time.sleep(.1)
            finally:
                backend.stop()
            ticks['recorder_stopped']=time.monotonic_ns()
            if not a.legacy: assert publisher._publisher is None
            reader=_open_reader(directory/'episode.mcap'); received=[]
            while reader.has_next():
                got_topic,data,receive_ns=reader.read_next()
                assert got_topic==topic
                m=deserialize_message(data,message_type)
                step=int(m.step_index) if a.topic=='infer' else int(m.sequence)-ep*a.messages-1
                assert 0<=step<a.messages
                assert int(m.monotonic_time_ns)==sent[step]['mono']
                if a.topic=='infer':
                    assert m.episode_id==ep_id and list(m.action)==[step/1000.]*14
                else:
                    assert m.reason==f'{ep_id}:{step}' and m.intervention_id==step and m.control_epoch==ep
                stamp=m.header.stamp.sec*1_000_000_000+m.header.stamp.nanosec
                received.append({'step':step,'send_ns':stamp,'receive_ns':receive_ns,'delay_ms':(receive_ns-stamp)/1e6})
            steps=[r['step'] for r in received]
            result={'episode':ep+1,'sent':len(sent),'received':len(received),'complete':steps==list(range(a.messages)),
                'missing':sorted(set(range(a.messages))-set(steps)),'max_delay_ms':max((r['delay_ms'] for r in received),default=None),'timing':ticks}
            for name,value in [('sent',sent),('received',received),('result',result)]:
                (directory/f'{name}.json').write_text(json.dumps(value,indent=2))
            summaries.append(result)
            summary={'topic':topic,'legacy':a.legacy,'episodes':len(summaries),'complete':sum(r['complete'] for r in summaries),
                'anomalies':[r['episode'] for r in summaries if not r['complete'] or (r['max_delay_ms'] or 0)>100],
                'sent':sum(r['sent'] for r in summaries),'received':sum(r['received'] for r in summaries),
                'max_delay_ms':max((r['max_delay_ms'] for r in summaries if r['max_delay_ms'] is not None),default=None)}
            (a.output/'summary.json').write_text(json.dumps(summary,indent=2))
            print(json.dumps(result),flush=True)
            if ep+1<a.episodes: time.sleep(1)
    print('GROUP_COMPLETE '+json.dumps(summary),flush=True)


if __name__=='__main__':main()
