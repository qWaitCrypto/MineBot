"""Unsupported benchmark goals must not create endless completion reviews."""
import asyncio
from types import SimpleNamespace

import pytest

from minebot.app.real_server_session import safe_evaluate_terminal_truth, _run_interactive_loop
from minebot.app.runtime_state import RuntimeStateStore, RuntimeScope, CheckpointDisposition, CompletionAuthority, TaskStatus
from minebot.app.session import SessionStep
from minebot.app.tasks import TaskWorkspace
from minebot.brain.lifecycle import LifecycleState
from minebot.contract import PerceptionResult

GOAL = '走到 [210,121,230]，然后回到空闲生活'

def workspace_for(path, goal=GOAL, *, request=False):
    store=RuntimeStateStore(path)
    workspace=TaskWorkspace(store, RuntimeScope('server','world','Bot'))
    task=workspace.start(goal,source='user')
    if request:
        workspace.checkpoint(expected_task_revision=task.revision,
            disposition=CheckpointDisposition.COMPLETE,summary='到达目标位置',
            evidence=['move_to arrived; native position [210.5,121,230.5]'])
    return workspace

class EmptyBody:
    def perceive(self,scope,params):
        return PerceptionResult('Bot',scope,'perception',True,True,{'slots':[]})

def session_for(workspace):
    return SimpleNamespace(task_workspace=workspace,
        parts=SimpleNamespace(runtime=SimpleNamespace(native_responses=False)))

def test_unsupported_goal_without_completion_request_is_not_accepted(tmp_path):
    workspace=workspace_for(tmp_path/'state.sqlite')
    truth=safe_evaluate_terminal_truth(EmptyBody(),GOAL,
        SessionStep('completed_turn',LifecycleState.ACTIVE),session=session_for(workspace))
    assert truth.target is None and truth.satisfied is False and truth.exit_code==7

def test_recorded_movement_completion_has_model_authority_not_fake_body_truth(tmp_path):
    workspace=workspace_for(tmp_path/'state.sqlite',request=True)
    truth=safe_evaluate_terminal_truth(EmptyBody(),GOAL,
        SessionStep('completed_turn',LifecycleState.ACTIVE),session=session_for(workspace))
    assert truth.target is None and truth.satisfied is True and truth.exit_code==0
    assert truth.facts['completion_authority']=='model'
    assert truth.facts['evidence']==['move_to arrived; native position [210.5,121,230.5]']
    assert workspace.current_task.status is TaskStatus.WAITING_EVENT

@pytest.mark.parametrize('goal,status', [('another goal','completed_turn'),(GOAL,'failed'),(GOAL,'quit')])
def test_model_checkpoint_does_not_complete_another_goal_or_override_session_terminal(tmp_path,goal,status):
    workspace=workspace_for(tmp_path/'state.sqlite',request=True)
    truth=safe_evaluate_terminal_truth(EmptyBody(),goal,
        SessionStep(status,LifecycleState.ACTIVE),session=session_for(workspace))
    assert not truth.satisfied

def test_available_body_checker_still_rejects_missing_inventory(tmp_path):
    workspace=workspace_for(tmp_path/'state.sqlite',goal='collect 1 dirt',request=True)
    truth=safe_evaluate_terminal_truth(EmptyBody(),'collect 1 dirt',
        SessionStep('completed_turn',LifecycleState.ACTIVE),session=session_for(workspace))
    assert truth.target is not None and not truth.satisfied and truth.inventory_count==0

@pytest.mark.parametrize('turns', [1, 2])
def test_interactive_loop_completes_once_and_announces_same_authority(tmp_path,monkeypatch,turns):
    workspace=workspace_for(tmp_path/'state.sqlite',request=True)
    task_id=workspace.current_task.task_id
    calls=[];announced=[]
    class Session:
        task_workspace=workspace
        current_goal=GOAL
        parts=SimpleNamespace(runtime=SimpleNamespace(native_responses=False))
        async def step(self):return SessionStep('completed_turn',LifecycleState.ACTIVE)
        def complete_current_goal(self,reason,*,authority):
            calls.append((reason,authority));workspace.complete(authority=authority)
            self.current_goal=''
            return SessionStep('completed',LifecycleState.IDLE)
    async def sampler(*args):await asyncio.Future()
    monkeypatch.setattr('minebot.app.real_server_session._trace_active_body_state_loop',sampler)
    monkeypatch.setattr('minebot.app.real_server_session._panel_idle_sample_loop',sampler)
    monkeypatch.setattr('minebot.app.real_server_session._poll_chat_commands',lambda *args:None)
    monkeypatch.setattr('minebot.app.real_server_session._announce_interactive_terminal',lambda body,truth:announced.append(truth))
    result=asyncio.run(_run_interactive_loop(Session(),fallback_goal=None,body=EmptyBody(),max_steps=turns))
    assert result.status==('completed' if turns==1 else 'completed_turn')
    assert calls==[('model_completion_requested',CompletionAuthority.MODEL)]
    assert workspace.current_task is None
    assert workspace.store.get_task(task_id).status is TaskStatus.COMPLETED
    assert workspace.store.get_task(task_id).completion_authority is CompletionAuthority.MODEL
    assert len(announced)==1 and announced[0].satisfied and announced[0].exit_code==0
    assert announced[0].facts['completion_authority']=='model'
