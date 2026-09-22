"""Fixed-window EXPO transport/client/scheduler; algorithms stay in expo-online-RL."""
from dataclasses import replace

from .models import InferenceTicket
from .openpi_transport import OpenPiDaggerTransport, policy_request_to_wire, policy_response_from_wire
from .policy_client import AsyncPi05PolicyClient, Pi05PolicyRequest, RtcPolicyContext
from .rtc_scheduler import RtcActionScheduler

VERSION = "arx5-expo-two-phase-v1"


class ExpoTransport(OpenPiDaggerTransport):
    def __init__(self, *args, bundle_id, **kwargs):
        super().__init__(*args, **kwargs)
        if self.metadata.get("expo_bundle_id") != bundle_id or self.metadata.get("expo_protocol") != VERSION:
            self.close()
            raise ValueError("EXPO server bundle/protocol mismatch")
        self.bundle_id=bundle_id

    def rpc(self, op, **fields):
        self._connection.send(self._codec.packb({"version":VERSION,"op":op,**fields}))
        response=self._recv_mapping()
        if response.get("version") != VERSION: raise ValueError("EXPO response version mismatch")
        return response["result"]


class ExpoPolicyClient(AsyncPi05PolicyClient):
    expo_two_phase = True

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._remote_episode=None

    def _capture_wire(self, episode, epoch, inference, rtc):
        self._require_active_epoch(epoch)
        step=self.observations.capture()
        observation=self.encoder.encode(step)
        request=Pi05PolicyRequest(self.session_id,episode,epoch,inference,
            self.checkpoint_sha256,self.prompt,observation,rtc)
        wire=policy_request_to_wire(request,self.transport._numpy)
        wire["observation"]["images"] = {k: self.transport._numpy.ascontiguousarray(v)
            for k, v in wire["observation"]["images"].items()}
        wire.update(expo_bundle_id=self.transport.bundle_id,observation_time_ns=step.cutoff_ns)
        return wire

    def _infer(self,episode_id,control_epoch,inference_id,rtc):
        identity=(self.session_id,episode_id,control_epoch)
        if self._remote_episode != identity:
            self.transport.rpc("begin",session_id=self.session_id,episode_id=episode_id,epoch=control_epoch)
            self._remote_episode=identity
        wire=self._capture_wire(episode_id,control_epoch,inference_id,rtc)
        prepared=self.transport.rpc("prepare",payload=wire)
        self._require_active_epoch(control_epoch)
        delay=0 if rtc is None else rtc.estimated_delay_steps
        if prepared.get("bundle_id")!=self.transport.bundle_id or prepared.get("delay")!=delay:
            raise ValueError("Prepared EXPO contract mismatch")
        if rtc is None: return self._finish(prepared,episode_id,control_epoch,inference_id,0)
        return prepared

    def finish(self,prepared,episode,epoch,inference,delay):
        self._require_active_epoch(epoch)
        return self._executor.submit(self._finish,prepared,episode,epoch,inference,delay)

    def _finish(self,prepared,episode,epoch,inference,delay):
        wire=self._capture_wire(episode,epoch,inference,None)
        value=self.transport.rpc("finish",ticket=prepared["ticket"],payload=wire,actual_delay=delay)
        if value.get("expo_bundle_id")!=self.transport.bundle_id:
            raise ValueError("EXPO finished bundle mismatch")
        if value.get("expo",{}).get("selected_window") != [delay,delay+self.execution.execution_steps]:
            raise ValueError("EXPO selected window mismatch")
        response=policy_response_from_wire(value)
        self._validate_response(response,episode,epoch,inference)
        self._require_active_epoch(epoch)
        return InferenceTicket(inference,epoch,self.checkpoint_sha256,response.action_chunk,self.execution)


class ExpoActionScheduler(RtcActionScheduler):
    """No early splice, no base tail execution, fixed C and d; reuse native action checks."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.c=self.checkpoint.execution.execution_steps
        self.d=self.rollout.initial_delay_steps
        if not 0<self.d<self.c or self.rollout.prefetch_after_steps!=self.c-self.d:
            raise ValueError("EXPO requires 0<d<C and prefetch_after_steps=C-d")
        if self.d+self.c>self.checkpoint.execution.action_chunk_size:
            raise ValueError("EXPO selected window exceeds horizon")
        self.safe_window_steps=self.c
        self._finishing=False

    def clear_pending(self,control_epoch):
        super().clear_pending(control_epoch)
        self._finishing=False

    def _step(self):
        now=self.clock()
        with self._lock:
            if not self._gate_open:return
            finishing=self._finishing
            pending=self._pending
            deadline=self._next_command_s
        if finishing:
            if deadline is not None and now>deadline:
                raise RuntimeError("EXPO fast-stage deadline missed")
            if pending is None:raise RuntimeError("Missing EXPO finish request")
            if not pending.future.done():return
            self._accept_pending(bootstrap_required=False)
            self._finishing=False
        with self._lock:
            if not self._gate_open or now<self._next_command_s:return
            if now-self._next_command_s>self.command_watchdog_s:
                raise RuntimeError("EXPO command watchdog expired")
            if not self._queue:raise RuntimeError("EXPO selected window exhausted")
            queued=self._queue.popleft()
            self.sink.publish(queued.command)
            self._issued_total+=1;self._issued_since_splice+=1
            self._next_command_s+=self.period_s
            if self._issued_since_splice==self.c-self.d:
                prefix=tuple(x.model_action for x in self._queue)
                if len(prefix)!=self.d:raise RuntimeError("EXPO prefix length mismatch")
                self._submit_locked(bootstrap=False,context=RtcPolicyContext(self.d,prefix))
            if self._issued_since_splice==self.c:
                pending=self._pending
                if pending is None or not pending.future.done():
                    raise RuntimeError("EXPO candidates missed fixed-delay boundary")
                prepared=pending.future.result()
                future=self.policy.finish(prepared,self._episode_id,self._control_epoch,pending.inference_id,self.d)
                self._pending=replace(pending,future=future)
                self._finishing=True

    def _run(self):
        while not self._stop.wait(min(self.period_s/8,0.001)):
            self.step()
