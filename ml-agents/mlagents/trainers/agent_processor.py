import sys
from typing import List, Dict, Deque, TypeVar, Generic
from collections import defaultdict, Counter, deque

from mlagents.trainers.trajectory import Trajectory, AgentExperience
from mlagents.trainers.brain import BrainInfo
from mlagents.trainers.tf_policy import TFPolicy
from mlagents.trainers.policy import Policy
from mlagents.trainers.action_info import ActionInfo, ActionInfoOutputs
from mlagents.trainers.stats import StatsReporter

T = TypeVar("T")


class AgentProcessor:
    """
    AgentProcessor contains a dictionary per-agent trajectory buffers. The buffers are indexed by agent_id.
    Buffer also contains an update_buffer that corresponds to the buffer used when updating the model.
    One AgentProcessor should be created per agent group.
    """

    def __init__(
        self,
        policy: TFPolicy,
        behavior_id: str,
        stats_reporter: StatsReporter,
        max_trajectory_length: int = sys.maxsize,
    ):
        """
        Create an AgentProcessor.
        :param trainer: Trainer instance connected to this AgentProcessor. Trainer is given trajectory
        when it is finished.
        :param policy: Policy instance associated with this AgentProcessor.
        :param max_trajectory_length: Maximum length of a trajectory before it is added to the trainer.
        :param stats_category: The category under which to write the stats. Usually, this comes from the Trainer.
        """
        self.experience_buffers: Dict[str, List[AgentExperience]] = defaultdict(list)
        self.last_brain_info: Dict[str, BrainInfo] = {}
        # last_take_action_outputs stores the action a_t taken before the current observation s_(t+1), while
        # grabbing previous_action from the policy grabs the action PRIOR to that, a_(t-1).
        self.last_take_action_outputs: Dict[str, ActionInfoOutputs] = {}
        # Note: In the future this policy reference will be the policy of the env_manager and not the trainer.
        # We can in that case just grab the action from the policy rather than having it passed in.
        self.policy = policy
        self.episode_steps: Counter = Counter()
        self.episode_rewards: Dict[str, float] = defaultdict(float)
        self.stats_reporter = stats_reporter
        self.max_trajectory_length = max_trajectory_length
        self.trajectory_queues: List[AgentManagerQueue[Trajectory]] = []
        self.behavior_id = behavior_id

    def add_experiences(
        self, curr_info: BrainInfo, previous_action: ActionInfo
    ) -> None:
        """
        Adds experiences to each agent's experience history.
        :param curr_info: current BrainInfo.
        :param previous_action: The return value of the Policy's get_action method.
        """
        take_action_outputs = previous_action.outputs
        if take_action_outputs:
            for _entropy in take_action_outputs["entropy"]:
                self.stats_reporter.add_stat("Policy/Entropy", _entropy)
            self.stats_reporter.add_stat(
                "Policy/Learning Rate", take_action_outputs["learning_rate"]
            )

        for agent_id in previous_action.agents:
            self.last_take_action_outputs[agent_id] = take_action_outputs

        # Store the environment reward
        tmp_environment_reward = curr_info.rewards

        for agent_idx, agent_id in enumerate(curr_info.agents):
            stored_info = self.last_brain_info.get(agent_id, None)
            stored_take_action_outputs = self.last_take_action_outputs.get(
                agent_id, None
            )
            if stored_info is not None and stored_take_action_outputs is not None:
                prev_idx = stored_info.agents.index(agent_id)
                obs = []
                if not stored_info.local_done[prev_idx]:
                    for i, _ in enumerate(stored_info.visual_observations):
                        obs.append(stored_info.visual_observations[i][prev_idx])
                    if self.policy.use_vec_obs:
                        obs.append(stored_info.vector_observations[prev_idx])
                    if self.policy.use_recurrent:
                        memory = self.policy.retrieve_memories([agent_id])[0, :]
                    else:
                        memory = None

                    done = curr_info.local_done[agent_idx]
                    max_step = curr_info.max_reached[agent_idx]

                    # Add the outputs of the last eval
                    action = stored_take_action_outputs["action"][prev_idx]
                    if self.policy.use_continuous_act:
                        action_pre = stored_take_action_outputs["pre_action"][prev_idx]
                    else:
                        action_pre = None
                    action_probs = stored_take_action_outputs["log_probs"][prev_idx]
                    action_masks = stored_info.action_masks[prev_idx]
                    prev_action = self.policy.retrieve_previous_action([agent_id])[0, :]

                    experience = AgentExperience(
                        obs=obs,
                        reward=tmp_environment_reward[agent_idx],
                        done=done,
                        action=action,
                        action_probs=action_probs,
                        action_pre=action_pre,
                        action_mask=action_masks,
                        prev_action=prev_action,
                        max_step=max_step,
                        memory=memory,
                    )
                    # Add the value outputs if needed
                    self.experience_buffers[agent_id].append(experience)
                    self.episode_rewards[agent_id] += tmp_environment_reward[agent_idx]
                if (
                    curr_info.local_done[agent_idx]
                    or (
                        len(self.experience_buffers[agent_id])
                        >= self.max_trajectory_length
                    )
                ) and len(self.experience_buffers[agent_id]) > 0:
                    # Make next AgentExperience
                    next_obs = []
                    for i, _ in enumerate(curr_info.visual_observations):
                        next_obs.append(curr_info.visual_observations[i][agent_idx])
                    if self.policy.use_vec_obs:
                        next_obs.append(curr_info.vector_observations[agent_idx])
                    trajectory = Trajectory(
                        steps=self.experience_buffers[agent_id],
                        agent_id=agent_id,
                        next_obs=next_obs,
                        behavior_id=self.behavior_id,
                    )
                    for traj_queue in self.trajectory_queues:
                        traj_queue.put(trajectory)
                    self.experience_buffers[agent_id] = []
                    if curr_info.local_done[agent_idx]:
                        self.stats_reporter.add_stat(
                            "Environment/Cumulative Reward",
                            self.episode_rewards.get(agent_id, 0),
                        )
                        self.stats_reporter.add_stat(
                            "Environment/Episode Length",
                            self.episode_steps.get(agent_id, 0),
                        )
                        del self.episode_steps[agent_id]
                        del self.episode_rewards[agent_id]
                elif not curr_info.local_done[agent_idx]:
                    self.episode_steps[agent_id] += 1

            self.last_brain_info[agent_id] = curr_info

        if "action" in take_action_outputs:
            self.policy.save_previous_action(
                previous_action.agents, take_action_outputs["action"]
            )

    def publish_trajectory_queue(
        self, trajectory_queue: "AgentManagerQueue[Trajectory]"
    ) -> None:
        """
        Adds a trajectory queue to the list of queues to publish to when this AgentProcessor
        assembles a Trajectory
        :param trajectory_queue: Trajectory queue to publish to.
        """
        self.trajectory_queues.append(trajectory_queue)


class AgentManagerQueue(Generic[T]):
    """
    Queue used by the AgentManager. Note that we make our own class here because in most implementations
    deque is sufficient and faster. However, if we want to switch to multiprocessing, we'll need to change
    out this implementation.
    """

    class Empty(Exception):
        """
        Exception for when the queue is empty.
        """

        pass

    def __init__(self, behavior_id: str):
        """
        Initializes an AgentManagerQueue. Note that we can give it a behavior_id so that it can be identified
        separately from an AgentManager.
        """
        self.queue: Deque[T] = deque()
        self.behavior_id = behavior_id

    def empty(self) -> bool:
        return len(self.queue) == 0

    def get_nowait(self) -> T:
        try:
            return self.queue.popleft()
        except IndexError:
            raise self.Empty("The AgentManagerQueue is empty.")

    def put(self, item: T) -> None:
        self.queue.append(item)


class AgentManager(AgentProcessor):
    """
    An AgentManager is an AgentProcessor that also holds a single trajectory and policy queue.
    Note: this leaves room for adding AgentProcessors that publish multiple trajectory queues.
    """

    def __init__(
        self,
        policy: TFPolicy,
        behavior_id: str,
        stats_reporter: StatsReporter,
        max_trajectory_length: int = sys.maxsize,
    ):
        super().__init__(policy, behavior_id, stats_reporter, max_trajectory_length)
        self.trajectory_queue: AgentManagerQueue[Trajectory] = AgentManagerQueue(
            self.behavior_id
        )
        self.policy_queue: AgentManagerQueue[Policy] = AgentManagerQueue(
            self.behavior_id
        )
        self.publish_trajectory_queue(self.trajectory_queue)
