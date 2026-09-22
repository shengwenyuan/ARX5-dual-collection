"""Manual pedal lifecycle without an EpisodeStore, MCAP writer, or command recorder."""
from time import monotonic_ns, time_ns
from uuid import uuid4
from arx5_collection.episode.models import RecordingStarted
from arx5_collection.episode.ports import TriggerEvent


class NoCommandPublisher:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def now_ns(self):return time_ns()
    def __call__(self,record):pass


def run_unrecorded(session,controller,pedals,episodes,status,*,streams):
    from .controller import InferRecordTrigger
    trigger=InferRecordTrigger(pedals,controller)
    completed=0
    try:
        while episodes==0 or completed<episodes:
            status("NOT RECORDING: right=HOME/start; Ctrl+C=exit")
            trigger.arm()
            try:
                while True:
                    session.supervisor.require_running()
                    signal=trigger.wait(0.02)
                    if signal and signal.event is TriggerEvent.ACTIVATE:break
            finally:trigger.disarm()
            session.pre_episode_check()
            session.home.run()
            session.monitor.start(streams)
            try:
                controller.start_episode(RecordingStarted('unrecorded-'+uuid4().hex,monotonic_ns()))
                trigger.arm()
                while True:
                    session.supervisor.require_running()
                    failure=session.monitor.required_failure()
                    if failure is not None:raise RuntimeError(f"infer stream failure: {failure}")
                    signal=trigger.wait(0.02)
                    if signal is not None:break
            finally:
                trigger.disarm()
                try:controller.close()
                finally:session.monitor.stop()
            completed+=1
        return 0
    finally:controller.close()
