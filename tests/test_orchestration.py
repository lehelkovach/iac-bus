from orchestration import (
    BarrierMode,
    BarrierSpec,
    Dependency,
    JobSpec,
    StepSpec,
    StepStatus,
    build_job_state,
    get_ready_steps,
    job_is_complete,
    mark_step_completed,
    update_timeouts,
)


def test_ready_steps_with_dependencies():
    job = JobSpec(
        job_id="job-1",
        name="demo",
        steps=[
            StepSpec(step_id="a"),
            StepSpec(step_id="b", depends_on=[Dependency(step_id="a")]),
        ],
    )
    state = build_job_state(job, now=100)

    ready = get_ready_steps(job, state, now=100)
    assert [step.step_id for step in ready] == ["a"]

    mark_step_completed(state, "a", now=110)
    ready = get_ready_steps(job, state, now=111)
    assert [step.step_id for step in ready] == ["b"]


def test_dependency_delay_gate():
    job = JobSpec(
        job_id="job-2",
        name="delay",
        steps=[
            StepSpec(step_id="a"),
            StepSpec(step_id="b", depends_on=[Dependency(step_id="a", min_delay_seconds=60)]),
        ],
    )
    state = build_job_state(job, now=100)

    mark_step_completed(state, "a", now=100)
    ready = get_ready_steps(job, state, now=150)
    assert [step.step_id for step in ready] == []

    ready = get_ready_steps(job, state, now=170)
    assert [step.step_id for step in ready] == ["b"]


def test_barrier_wait_for():
    job = JobSpec(
        job_id="job-3",
        name="barrier",
        steps=[
            StepSpec(step_id="a"),
            StepSpec(step_id="b"),
            StepSpec(step_id="c", wait_for=["sync-1"]),
        ],
        barriers=[
            BarrierSpec(barrier_id="sync-1", requires=["a", "b"], mode=BarrierMode.ALL_COMPLETED),
        ],
    )
    state = build_job_state(job, now=200)

    ready = get_ready_steps(job, state, now=200)
    assert [step.step_id for step in ready] == ["a", "b"]

    mark_step_completed(state, "a", now=210)
    ready = get_ready_steps(job, state, now=211)
    assert [step.step_id for step in ready] == ["b"]

    mark_step_completed(state, "b", now=220)
    ready = get_ready_steps(job, state, now=221)
    assert [step.step_id for step in ready] == ["c"]


def test_deadline_timeout():
    job = JobSpec(
        job_id="job-4",
        name="deadline",
        steps=[
            StepSpec(step_id="a", deadline_ts=150),
        ],
    )
    state = build_job_state(job, now=100)
    timed_out = update_timeouts(job, state, now=200)
    assert timed_out == ["a"]
    assert state.step_states["a"].status == StepStatus.TIMED_OUT
    assert job_is_complete(job, state)
