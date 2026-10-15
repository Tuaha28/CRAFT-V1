"""Canonical feasibility, materialization, replay, and validation pipeline."""
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
import hashlib
import json
import os
from pathlib import Path
import pickle
import time

from ortools.sat.python import cp_model

from crafter.phenotype import validate
from crafter.disruptions.state_update import event_conflicts
from crafter.lns_probe.joint_modes import JointExecutableMode
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode
from .calendar_regimes import _execution
from .cp_repair import build_repair_model
from .objectives import (ManagerPriorityInputError, add_objective_variables,
                         baseline_objective_metrics, build_objective_context,
                         expression_is_constant, selected_objective_metrics)


class DiagnosticFirstUnknown(RuntimeError):
    """Raised only by the diagnostic first-UNKNOWN checkpoint mode."""

    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        window_id = (checkpoint or {}).get("window_id", "unknown-window")
        super().__init__(f"diagnostic first UNKNOWN captured for {window_id}")


def _serialize_cp_model_proto(proto):
    """Return a stable, replayable representation across OR-Tools bindings.

    Recent OR-Tools Python bindings expose CpModelProto as a pybind object
    without protobuf's SerializeToString method.  Its canonical text form is
    still lossless and can be parsed by the same binding, so use it as the
    diagnostic wire representation when binary protobuf serialization is not
    available.
    """
    serializer = getattr(proto, "SerializeToString", None)
    if callable(serializer):
        return serializer()
    return str(proto).encode("utf-8")


def _signature(config):
    return (int(config.line_id),int(config.k),tuple(config.machine_ids),tuple(config.worker_ids))


def _anchor(state,key,configs):
    # A first INITIAL_PLAN has no prior provisional schedule.  Existing
    # database rows may describe historical/completed state, but they must not
    # become a hidden stability reference or steer the candidate stages.
    if state.get('objective_profile') == 'INITIAL_PLAN_NO_STABILITY':
        fallback=sorted(configs,key=_signature)[0]
        return fallback,{'status':'NO_PREVIOUS_PLAN_DETERMINISTIC_ANCHOR',
                         'fallback_signature':_signature(fallback)}
    item=next(x for x in state['baseline'].operations if x.assignment.operation_key==key)
    wanted=(int(item.assignment.line_id),int(item.assignment.machine_count),
            tuple(sorted(int(x) for x in item.assignment.machine_ids)),
            tuple(sorted(str(x) for x in item.employee_ids)))
    exact=next((c for c in configs if _signature(c)==wanted),None)
    if exact is not None:
        return exact,{'status':'EXACT_INCUMBENT_CONFIGURATION','signature':wanted}
    fallback=sorted(configs,key=_signature)[0]
    return fallback,{'status':'INCUMBENT_CONFIGURATION_ABSENT_DETERMINISTIC_FALLBACK',
                     'requested_signature':wanted,'fallback_signature':_signature(fallback)}


def _diverse(configs,anchor,limit):
    selected=[anchor]; remaining=[c for c in configs if c!=anchor]
    while remaining and len(selected)<limit:
        def score(c):
            distance=min(len(set(c.worker_ids)^set(s.worker_ids))*4+
                         len(set(c.machine_ids)^set(s.machine_ids))*3+
                         (c.k!=s.k)*2+(c.line_id!=s.line_id)*2 for s in selected)
            return distance,_signature(c)
        choice=max(remaining,key=score); selected.append(choice); remaining.remove(choice)
    return tuple(selected)


def _restrict(state,regimes,selected,window_days):
    from .engine import _routing_aware_earliest
    bounds=_routing_aware_earliest(state,regimes); out={}
    for key in sorted(state['keys']):
        allowed=set(selected[key]); lower=int(bounds['earliest_start'].get(key,0))
        upper=int(state['problem'].horizon_seconds)
        if window_days is not None: upper=min(upper,lower+int(window_days*86400))
        out[key]=tuple({**r,'lo':max(int(r['lo']),lower),'hi':min(int(r['hi']),upper)}
                       for r in regimes[key] if r['config'] in allowed
                       and max(int(r['lo']),lower)<=min(int(r['hi']),upper))
    return out,bounds


def _hints(state,regimes,artifact,bounds,incumbent_selected=None):
    rows=[]
    incumbent = {str(row.get('operation_key')): row
                 for row in (incumbent_selected or ())}
    for key in sorted(state['keys']):
        wanted=int(bounds['earliest_start'].get(key,0))
        preferred = incumbent.get(str(key))
        options=[]
        if preferred is not None:
            cfg = preferred.get('resource_configuration', {})
            signature=(int(cfg.get('line_id',-1)), int(cfg.get('k',-1)),
                       tuple(int(x) for x in cfg.get('machine_ids',())),
                       tuple(str(x) for x in cfg.get('worker_ids',())))
            options=[(i,r) for i,r in enumerate(regimes[key])
                     if _signature(r['config']) == signature
                     and int(r['lo']) <= int(preferred.get('start_seconds',wanted)) <= int(r['hi'])]
            if options:
                wanted=int(preferred.get('start_seconds', wanted))
        if not options:
            options=[(i,r) for i,r in enumerate(regimes[key]) if int(r['lo'])<=wanted<=int(r['hi'])]
        if not options: continue
        i,r=options[0]; a,b=r['formula'][0]
        artifact['model'].AddHint(artifact['reg_lit'][key][i],1)
        artifact['model'].AddHint(artifact['starts'][key],wanted)
        artifact['model'].AddHint(artifact['ends'][key],int(a)*wanted+int(b))
        rows.append({'operation_key':key,'regime_index':i,'start_seconds':wanted})
    return rows


def _solution_hints(artifact, solver):
    """Replace hints with a complete legal incumbent from an earlier solve."""
    model = artifact['model']
    if hasattr(model, 'ClearHints'):
        model.ClearHints()
    for key in sorted(artifact['starts']):
        model.AddHint(artifact['starts'][key], solver.Value(artifact['starts'][key]))
        model.AddHint(artifact['ends'][key], solver.Value(artifact['ends'][key]))
        for lit in artifact['reg_lit'][key]:
            model.AddHint(lit, solver.Value(lit))


def _solver():
    solver=cp_model.CpSolver()
    solver.parameters.num_search_workers=8
    solver.parameters.random_seed=42
    solver.parameters.use_optimization_hints=True
    solver.parameters.cp_model_presolve=True
    return solver


def _adaptive_cp_budget_ladder(initial_budget):
    """Return the production CP budget ladder without changing the model.

    The first value is the caller's normal stage budget.  Higher budgets are
    consulted only after UNKNOWN with no incumbent.  The ladder is opt-in via
    CRAFT_ADAPTIVE_CP_BUDGETS so research callers retain their historical
    single-attempt behavior unless explicitly enabled.
    """
    if os.environ.get("CRAFT_ADAPTIVE_CP_BUDGETS", "0") != "1":
        return (float(initial_budget),)
    configured = os.environ.get("CRAFT_ADAPTIVE_CP_RETRY_SECONDS", "120,240")
    retries = []
    for token in configured.split(","):
        try:
            value = float(token.strip())
        except (TypeError, ValueError):
            continue
        if value > float(initial_budget) and value not in retries:
            retries.append(value)
    return (float(initial_budget), *retries)


def _solve_stage_with_adaptive_budget(artifact, initial_budget, *, checkpoint_args=None):
    """Solve one already-built CP model with UNKNOWN-only budget escalation.

    The artifact, ResourceConfigurations, CalendarExecutionRegimes and model
    proto are not rebuilt between attempts.  A fresh solver object is used for
    each budget, but it receives the same canonical model and the same
    parameters from ``_solver``.
    """
    model = artifact["model"]
    attempts = []
    winning_solver = None
    winning_row = None
    for attempt_index, budget in enumerate(_adaptive_cp_budget_ladder(initial_budget), 1):
        solver = _solver()
        solver.parameters.max_time_in_seconds = float(budget)
        if os.environ.get("CRAFT_DIAGNOSTIC_CHECKPOINT_DIR"):
            solver.parameters.log_search_progress = True
        checkpoint_metadata = None
        if checkpoint_args is not None:
            checkpoint_metadata = _diagnostic_checkpoint_before_solve(
                checkpoint_args["prepared"], artifact,
                stage=checkpoint_args["stage"], plan=checkpoint_args["plan"],
                objective_mode=checkpoint_args["objective_mode"],
                objective_time_limit_seconds=checkpoint_args["objective_time_limit_seconds"],
                manager_overrides=checkpoint_args["manager_overrides"], solver=solver)
        started = time.perf_counter()
        code = solver.Solve(model)
        elapsed = time.perf_counter() - started
        if (code == cp_model.UNKNOWN
                and isinstance(checkpoint_metadata, dict)
                and checkpoint_metadata.get("_deferred")):
            persisted = _persist_diagnostic_checkpoint(
                checkpoint_metadata["root"], checkpoint_metadata["prepared"],
                checkpoint_metadata["proto_bytes"], checkpoint_metadata["metadata"])
            if os.environ.get("CRAFT_DIAGNOSTIC_STOP_ON_FIRST_UNKNOWN", "0") == "1":
                raise DiagnosticFirstUnknown(persisted)
            checkpoint_metadata = persisted
        response = solver.ResponseProto()
        row = {
            "attempt": attempt_index,
            "time_limit_seconds": float(budget),
            "status": solver.StatusName(code),
            "solve_seconds": solver.WallTime(),
            "elapsed_seconds": elapsed,
            "deterministic_time": getattr(response, "deterministic_time", None),
            "branches": solver.NumBranches(),
            "conflicts": solver.NumConflicts(),
            "candidate_exists": code in (cp_model.FEASIBLE, cp_model.OPTIMAL),
            "objective": solver.ObjectiveValue(),
            "best_bound": solver.BestObjectiveBound(),
            "diagnostic_checkpoint": checkpoint_metadata,
        }
        attempts.append(row)
        if code in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            winning_solver, winning_row = solver, row
            break
        # UNKNOWN with no candidate is the only status eligible for budget
        # escalation.  INFEASIBLE remains an exact result for this attempted
        # model/domain and is not reinterpreted as a search timeout.
        if code != cp_model.UNKNOWN:
            break
    if winning_row is None and attempts:
        winning_row = attempts[-1]
    return winning_solver, winning_row, attempts


def _diagnostic_checkpoint_before_solve(prepared, artifact, *, stage, plan,
                                        objective_mode, objective_time_limit_seconds,
                                        manager_overrides, solver):
    """Capture or persist an exact, diagnostic-only CP attempt.

    In first-UNKNOWN mode the payload remains in memory until Solve() returns
    UNKNOWN without an incumbent; this avoids serializing every successful
    rolling window.  The prepared state/domain is pickled, while the canonical
    CpModelProto is stored separately so replay can verify the model byte-for-
    byte before solving at alternate budgets.
    """
    root = os.environ.get("CRAFT_DIAGNOSTIC_CHECKPOINT_DIR")
    target = os.environ.get("CRAFT_DIAGNOSTIC_CHECKPOINT_WINDOW")
    context = (prepared.get("state", {}) or {}).get("planning_context", {}) or {}
    if not root or (target and str(context.get("window_id", "")) != str(target)):
        return None
    proto_bytes = _serialize_cp_model_proto(artifact["model"].Proto())
    params = solver.parameters
    metadata = {
        "window_id": context.get("window_id") or context.get("planning_context_id"),
        "planning_context": context,
        "stage": int(stage),
        "plan": [list(item) for item in plan],
        "objective_mode": bool(objective_mode),
        "objective_time_limit_seconds": float(objective_time_limit_seconds),
        "manager_overrides": list(manager_overrides or ()),
        "solver_parameters": {
            "num_search_workers": int(params.num_search_workers),
            "random_seed": int(params.random_seed),
            "cp_model_presolve": bool(params.cp_model_presolve),
            "use_optimization_hints": bool(params.use_optimization_hints),
            "log_search_progress": bool(params.log_search_progress),
            "max_time_in_seconds": float(params.max_time_in_seconds),
        },
        "resource_configuration_count": sum(len(v) for v in (prepared.get("resource_configurations") or {}).values()),
        "regime_count": int(prepared.get("regime_count", 0)),
        "required_operation_keys": list(prepared.get("required_window_keys") or context.get("selected_operation_keys") or ()),
        "model_sha256": hashlib.sha256(proto_bytes).hexdigest(),
        "model_bytes": len(proto_bytes),
    }
    if os.environ.get("CRAFT_DIAGNOSTIC_FIRST_UNKNOWN", "0") == "1":
        return {"_deferred": True, "root": root, "prepared": prepared,
                "proto_bytes": proto_bytes, "metadata": metadata}
    return _persist_diagnostic_checkpoint(root, prepared, proto_bytes, metadata)


def _persist_diagnostic_checkpoint(root, prepared, proto_bytes, metadata):
    """Write one captured checkpoint and append its manifest entry."""
    out = Path(root)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "checkpoint_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"checkpoints": []}
    except Exception:
        manifest = {"checkpoints": []}
    index = len(manifest.get("checkpoints", []))
    model_sha = hashlib.sha256(proto_bytes).hexdigest()
    stem = f"checkpoint_{index:03d}_stage_{int(metadata.get('stage', 0))}"
    prepared_path = out / f"{stem}.prepared.pkl"
    proto_path = out / f"{stem}.cp_model.pb"
    metadata_path = out / f"{stem}.json"
    # Pickle is used only for this local diagnostic checkpoint.  A failure to
    # persist must not alter the scheduling attempt; the metadata records it.
    pickle_error = None
    try:
        with prepared_path.open("wb") as handle:
            pickle.dump(prepared, handle, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:  # pragma: no cover - depends on runtime objects
        pickle_error = f"{type(exc).__name__}: {exc}"
        if prepared_path.exists():
            prepared_path.unlink()
    proto_path.write_bytes(proto_bytes)
    metadata = {
        **metadata,
        "checkpoint_index": index,
        "model_sha256": model_sha,
        "model_bytes": len(proto_bytes),
        "prepared_pickle": prepared_path.name if pickle_error is None else None,
        "model_proto": proto_path.name,
        "pickle_error": pickle_error,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    manifest.setdefault("checkpoints", []).append(metadata)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return metadata


def _value(solver, expression):
    return int(round(float(solver.Value(expression)))) if not isinstance(expression, int) else int(expression)


def solve_lexicographic_objectives(state, artifact, feasible_solver, *, manager_overrides=(),
                                   time_limit_seconds=30, deadline=None):
    """Optimize the V1 business vector without weakening canonical feasibility.

    Each successful level is proven OPTIMAL and frozen before the next level.
    A FEASIBLE/UNKNOWN lower level retains the last legal incumbent and is
    explicitly reported as partial rather than mislabelled as an optimum.
    """
    context=build_objective_context(state, manager_overrides)
    objective=add_objective_variables(state, artifact, context)
    before=baseline_objective_metrics(state, context)
    model=artifact['model']; incumbent=feasible_solver; stages=[]; partial=False
    _solution_hints(artifact, incumbent)
    for item in objective['expressions']:
        expression=item['expression']
        row={k:v for k,v in item.items() if k != 'expression'}
        # The objective stages deliberately reuse the same canonical CP model.
        # Persist its size on every row so runtime evidence cannot be mistaken
        # for a different formulation or an unrecorded rebuild.
        proto = model.Proto()
        row.update({
            'time_limit_seconds': float(time_limit_seconds),
            # Objective variables and their exact linking constraints have
            # already been attached.  Count this actual proto, while also
            # retaining the canonical pre-objective size for diagnosis.
            'model_variables': len(proto.variables),
            'model_constraints': len(proto.constraints),
            'model_optional_intervals': int(artifact.get('size', {}).get('optional_intervals', 0)),
            'canonical_model_variables': int(artifact.get('size', {}).get('variables', 0)),
            'canonical_model_constraints': int(artifact.get('size', {}).get('constraints', 0)),
        })
        if expression_is_constant(expression):
            row.update({'status':'SKIPPED_CONSTANT','value':int(expression),'best_bound':int(expression),
                        'runtime_seconds':0.0,'solver_wall_seconds':0.0,
                        'branches':0,'conflicts':0,'proven_optimal':True})
            stages.append(row); continue
        remaining = (float(deadline) - time.perf_counter()
                     if deadline is not None else float(time_limit_seconds))
        if remaining <= 0.0:
            row.update({'status':'SEARCH_LIMIT_WITH_VALID_INCUMBENT',
                        'runtime_seconds':0.0,'solver_wall_seconds':0.0,
                        'best_bound':None,'branches':0,'conflicts':0,
                        'value':None,'proven_optimal':False,
                        'deadline_exhausted':True})
            partial=True; stages.append(row); break
        model.ClearObjective(); model.Minimize(expression)
        solver=_solver(); solver.parameters.max_time_in_seconds=float(
            min(float(time_limit_seconds), remaining))
        started=time.perf_counter(); code=solver.Solve(model); elapsed=time.perf_counter()-started
        row.update({'status':solver.StatusName(code),'runtime_seconds':elapsed,
                    'solver_wall_seconds':solver.WallTime(),'best_bound':float(solver.BestObjectiveBound()),
                    'branches':solver.NumBranches(),'conflicts':solver.NumConflicts()})
        if code == cp_model.OPTIMAL:
            optimum=_value(solver, expression)
            row.update({'value':optimum,'proven_optimal':True})
            model.Add(expression == optimum)
            incumbent=solver; _solution_hints(artifact, incumbent)
            stages.append(row); continue
        partial=True
        if code == cp_model.FEASIBLE:
            incumbent=solver
            row['value']=_value(solver, expression)
            _solution_hints(artifact, incumbent)
        else:
            row['value']=None
        row['proven_optimal']=False
        stages.append(row)
        break
    model.ClearObjective()
    return {'solver':incumbent,'objective':objective,'before_metrics':before,'stages':stages,
            'status':'OBJECTIVE_OPTIMIZATION_PARTIAL' if partial else 'OBJECTIVE_OPTIMIZATION_COMPLETE'}


def _materialize(state,artifact,solver):
    modes={}; selected=[]; checks=[]; origin=state['problem'].origin
    for key in sorted(state['keys']):
        ri=next(i for i,lit in enumerate(artifact['reg_lit'][key]) if solver.Value(lit))
        regime=artifact['reg_info'][(key,ri)]; config=regime['config']
        start=solver.Value(artifact['starts'][key]); cp_end=solver.Value(artifact['ends'][key])
        authoritative_end,segments=_execution(state,config,start); f=regime['formula']
        cp_segments=tuple((int(f[1+2*i][0])*start+int(f[1+2*i][1]),
                           int(f[2+2*i][0])*start+int(f[2+2*i][1]))
                          for i in range(int(regime['segments'])))
        mismatch=[]
        if authoritative_end!=cp_end: mismatch.append('END')
        if tuple(segments)!=cp_segments: mismatch.append('WORKER_SEGMENTS')
        checks.append({'operation_key':key,'mismatches':mismatch,'cp_end':cp_end,
                       'authoritative_end':authoritative_end,'cp_segments':cp_segments,
                       'authoritative_segments':segments})
        dt_segments=tuple((origin+timedelta(seconds=a),origin+timedelta(seconds=b)) for a,b in cp_segments)
        productive_start=start+int(config.setup_seconds)
        modes[key]=JointExecutableMode(
            f'canonical_{key}_{ri}',key,int(config.factory_id),int(config.line_id),int(config.k),
            tuple(config.machine_ids),tuple(config.worker_ids),start,productive_start,
            int(config.setup_seconds),int(config.productive_seconds),
            float(config.productive_minutes or config.productive_seconds/60.0),cp_end,cp_end-start,
            max(0,cp_end-productive_start-int(config.productive_seconds)),dt_segments,(),(),
            tuple(config.provenance),False,start,cp_end,'CANONICAL_REPAIR','CP_SELECTED_EXACT_REGIME',
            getattr(state.get('event'), 'event_id', None))
        selected.append({'operation_key':key,'regime_index':ri,'start_seconds':start,
                         'end_seconds':cp_end,'resource_configuration':asdict(config),
                         'worker_segments':segments})
    return modes,selected,checks


def _factory_isolation(state,selected):
    index=state.get('validation_operator_index',state['operator_index']); worker_factories={}
    for (factory,_skill),workers in index.get('by_factory',{}).items():
        for worker in workers: worker_factories.setdefault(str(worker),set()).add(int(factory))
    violations=[]
    for row in selected:
        cfg=row['resource_configuration']; factory=int(cfg['factory_id']); line=int(cfg['line_id'])
        if int(state['problem'].line_by_id[line].factory_id)!=factory:
            violations.append({'operation_key':row['operation_key'],'type':'LINE_FACTORY'})
        for mid in cfg['machine_ids']:
            machine=state['problem'].machine_by_id[int(mid)]
            if int(machine.factory_id)!=factory or int(machine.line_id)!=line:
                violations.append({'operation_key':row['operation_key'],'type':'MACHINE_FACTORY_LINE','machine_id':mid})
        for worker in cfg['worker_ids']:
            if factory not in worker_factories.get(str(worker),set()):
                violations.append({'operation_key':row['operation_key'],'type':'WORKER_FACTORY','worker_id':worker})
    return violations


def _accept(state,modes,selected,materialization):
    material_count=sum(bool(x['mismatches']) for x in materialization)
    replay_rows=[]; replay_mismatches=[]
    neighborhood=SimpleNamespace(operation_keys=tuple(sorted(state['keys'])))
    if material_count==0:
        for key,mode in modes.items():
            end,segments,mismatches=_replay_mode(
                state['problem'], state['operator_index'], state['after_index'],
                neighborhood, mode,
                worker_reservations=state.get('_worker_res'))
            replay_rows.append({'operation_key':key,'selected_end_seconds':mode.end_seconds,
                                'replayed_end_seconds':end,'exact':not mismatches,'mismatches':mismatches})
            replay_mismatches.extend({'operation_key':key,**m} for m in mismatches)
    replay={'status':'PASS' if material_count==0 and not replay_mismatches else 'FAIL',
            'mismatch_count':len(replay_mismatches),'rows':replay_rows,'mismatches':replay_mismatches,
            'path':'crafter.lns_probe.run_e1_7_final_verified._replay_mode'}
    validator={'status':'NOT_REACHED'}; event_violations=[]
    if replay['status']=='PASS':
        # Planning windows deliberately remove FUTURE_OUTSIDE_WINDOW rows from
        # the reservation view used by CP and replay.  Validating them again
        # by overlaying the selected rows onto the historical full baseline
        # would reintroduce those mutable rows as scheduled work and report
        # false worker/precedence overlaps.  Use the existing scoped planning
        # validator for that state, while retaining the full phenotype path
        # for disruption/repair states that intentionally validate a complete
        # incumbent phenotype.
        if state.get('planning_context') is not None:
            from .scoped_validation import validate_canonical_planning_candidate
            classified = state.get('classified') or {}
            future_outside = tuple(sorted(
                str(key) for key, item in classified.items()
                if isinstance(item, dict) and item.get('state') == 'FUTURE_OUTSIDE_WINDOW'))
            candidate = {
                'state': state,
                'acceptance': {
                    'selected_schedule': selected,
                    'materialization': {'mismatch_count': material_count},
                },
            }
            scoped = validate_canonical_planning_candidate(
                candidate, state,
                required_operation_keys=tuple(sorted(state.get('keys', ()))),
                boundary_facts=tuple(state.get('boundary_facts', ())),
                future_outside_operation_keys=future_outside)
            physical = scoped.get('physical') or {}
            physical_status = scoped.get('physical_status')
            validator={'status':'VALID' if physical_status == 'VALID' else 'VALIDATION_FAILED',
                       'phenotype_status':physical_status,
                       'validation_report':physical.get('report'),
                       'scope_acceptance':scoped}
        else:
            phenotype=_phenotype(state['problem'],state['baseline'],modes)
            checked=validate(
                state['problem'], phenotype,
                state.get('validation_operator_index', state['operator_index']),
                execution_contracts=state.get('operation_execution_contracts'),
                allow_test_assumptions=bool(state.get('allow_test_execution_assumptions', False)),
                require_execution_contracts=bool(state.get('require_execution_contracts', False)),
                worker_interval_is_available=state.get('worker_interval_is_available'),
                machine_interval_is_available=state.get('machine_interval_is_available'),
                expected_source_fingerprint=(state.get('source_resource_provenance') or {}).get('source_sha256'),
                expected_snapshot_fingerprint=(state.get('source_resource_provenance') or {}).get('snapshot_hash'),
                expected_scenario_fingerprint=(state.get('source_resource_provenance') or {}).get('scenario_hash'))
            validator={'status':'VALID' if checked.status.value=='VALIDATED_FEASIBLE' else checked.status.value,
                       'phenotype_status':checked.status.value,'validation_report':checked.validation_report}
        # Planning contexts have no synthetic disruption event.  They still
        # pass the identical replay and independent-validator path.
        event_violations = ([] if state.get('event') is None
                            else event_conflicts(state['event'],phenotype,state['problem']))
    isolation=_factory_isolation(state,selected)
    return replay,validator,event_violations,isolation


def run_canonical_schedule(prepared, *, plan=((1,None,30),(2,7,180),(3,7,240),(999,14,300)),
                           objective_mode=True, manager_overrides=(),
                           objective_time_limit_seconds=30,
                           incumbent_selected_schedule=None,
                           unrestricted_final_stage=False):
    """Shared canonical bounded-state scheduler.

    The prepared state may originate from disruption repair or truthful
    planning.  This function intentionally owns no event mutation, causal
    closure, or window-selection policy; it owns only CP through acceptance.
    """
    if prepared.get('escalation'): return prepared
    from .fast_mode import (fast_mode_config, fast_search_plan,
                            deadline_stage_budget, shared_attempt_deadline)
    fast = fast_mode_config()
    if fast.enabled and plan == ((1,None,30),(2,7,180),(3,7,240),(999,14,300)):
        plan = fast_search_plan(adaptive=bool(prepared.get("adaptive_recovery_attempt")))
    started=time.perf_counter(); state=prepared['state']; configs=prepared['resource_configurations']; regimes=prepared['regimes']
    # Fast mode owns one absolute deadline for the complete canonical attempt.
    # It is deliberately shared by all bounded stages; a later stage can never
    # receive a fresh 60/90 second allowance.  The outer rolling watchdog, when
    # supplied by the runner, is an additional (earlier) ceiling.
    attempt_deadline = None
    attempt_budget = None
    if fast.enabled:
        attempt_kind = ("adaptive" if prepared.get("adaptive_recovery_attempt")
                        else "normal")
        attempt_budget = (fast.adaptive_solve_limit_seconds if attempt_kind == "adaptive"
                          else fast.normal_solve_limit_seconds)
        # Nested V2/V3/temporal controllers reuse this state.  The deadline is
        # created once per normal or adaptive attempt, rather than once per
        # temporal plan or resource-domain retry.
        attempt_deadline = shared_attempt_deadline(state, attempt_budget, attempt_kind,
                                                   now=started)
        outer_deadline = state.get("_craft_window_deadline")
        if outer_deadline is not None:
            attempt_deadline = min(float(attempt_deadline), float(outer_deadline))
    anchors={}; provenance={}
    for key in sorted(state['keys']): anchors[key],provenance[key]=_anchor(state,key,configs[key])
    stages=[]; solution=None
    for stage,(limit,window,seconds) in enumerate(plan,1):
        before_stage = deadline_stage_budget(attempt_deadline, seconds)
        if attempt_deadline is not None and before_stage <= 0.0:
            stages.append({'stage':stage,'status':'SEARCH_LIMIT_NO_INCUMBENT',
                           'config_limit':limit,'window_days':window,
                           'deadline_remaining_before_stage':0.0,
                           'search_limit_hit':True,'skipped_due_deadline':True})
            break
        chosen={k:_diverse(configs[k],anchors[k],min(limit,len(configs[k]))) for k in sorted(state['keys'])}
        use_unrestricted = bool(unrestricted_final_stage and window is None and
                                limit >= max((len(configs[k]) for k in state['keys']), default=0))
        if use_unrestricted:
            # This is a temporal widening stage, not a diagnostic relaxation:
            # the exact canonical model still contains routing, calendars,
            # eligibility, reservations, setup, and all no-overlap rules.  The
            # only difference is that the stage does not apply the heuristic
            # earliest/start-region clipping used by bounded stages.
            stage_regimes={k:tuple(regimes[k]) for k in sorted(state['keys'])}
            from .engine import _routing_aware_earliest
            bounds=_routing_aware_earliest(state, regimes)
        else:
            stage_regimes,bounds=_restrict(state,regimes,chosen,window)
        if any(not rows for rows in stage_regimes.values()):
            stages.append({'stage':stage,'status':'ZERO_DOMAIN_AFTER_WINDOW','config_limit':limit,'window_days':window}); continue
        build_started=time.perf_counter(); artifact=build_repair_model(state,stage_regimes); build_seconds=time.perf_counter()-build_started; validation=artifact['model'].Validate()
        if validation: raise RuntimeError(f'invalid canonical repair model: {validation}')
        hints=_hints(state,stage_regimes,artifact,bounds,incumbent_selected_schedule)
        budget = deadline_stage_budget(attempt_deadline, seconds)
        if attempt_deadline is not None and budget <= 0.0:
            stages.append({'stage':stage,'status':'SEARCH_LIMIT_NO_INCUMBENT',
                           'config_limit':limit,'window_days':window,
                           'deadline_remaining_before_stage':before_stage,
                           'deadline_remaining_after_build':0.0,
                           'search_limit_hit':True,'skipped_due_deadline':True,
                           'model_build_seconds':build_seconds})
            break
        checkpoint_args = {
            "prepared": prepared, "stage": stage, "plan": plan,
            "objective_mode": objective_mode,
            "objective_time_limit_seconds": objective_time_limit_seconds,
            "manager_overrides": manager_overrides,
        }
        solver, solve_row, budget_attempts = _solve_stage_with_adaptive_budget(
            artifact, budget, checkpoint_args=checkpoint_args)
        row_checkpoint = (budget_attempts[0].get("diagnostic_checkpoint")
                          if budget_attempts else None)
        final_attempt = budget_attempts[-1] if budget_attempts else {}
        code = (cp_model.FEASIBLE if solver is not None and
                final_attempt.get("status") == "FEASIBLE" else
                cp_model.OPTIMAL if solver is not None and
                final_attempt.get("status") == "OPTIMAL" else
                cp_model.UNKNOWN if final_attempt.get("status") == "UNKNOWN" else
                cp_model.INFEASIBLE)
        solve_elapsed = float(final_attempt.get("elapsed_seconds", 0.0) or 0.0)
        deterministic_time = final_attempt.get("deterministic_time")
        solver_wall_time = float(final_attempt.get("solve_seconds", 0.0) or 0.0)
        boolean_variables = (solver.NumBooleans()
                             if solver is not None and hasattr(solver, 'NumBooleans')
                             else None)
        row={'stage':stage,'config_limit':limit,'window_days':window,
             'resource_configurations':sum(len(x) for x in chosen.values()),
             'regimes':sum(len(x) for x in stage_regimes.values()),'hint_count':len(hints),
             'model_validation':'PASS','status':final_attempt.get('status', 'UNKNOWN'),
             'solve_seconds':solver_wall_time,
             'deadline_remaining_before_stage':before_stage,
             'deadline_budget_seconds':float(final_attempt.get('time_limit_seconds', budget)),
             'search_limit_hit':bool(attempt_deadline is not None and
                                     (code == cp_model.UNKNOWN or
                                     solve_elapsed + build_seconds >= before_stage - 1e-3)),
             'model_build_seconds':build_seconds,'cp_model_build_seconds':build_seconds,
             'cp_presolve_seconds':None,'cp_solve_seconds':solver_wall_time,
             'wall_time':solver_wall_time,'deterministic_time':deterministic_time,
             'boolean_variables':boolean_variables,'interval_variables':None,
             'no_overlap_constraints':None,'branches':final_attempt.get('branches', 0),
             'conflicts':final_attempt.get('conflicts', 0),
             'candidate_exists':bool(solver is not None),
             'objective':final_attempt.get('objective'),
             'best_bound':final_attempt.get('best_bound'),
             'cp_budget_attempts':budget_attempts,
             'cp_budget_escalations':max(0, len(budget_attempts) - 1),
             'cp_budget_solved_at':(final_attempt.get('time_limit_seconds')
                                    if solver is not None else None),
             'time_scope':'UNRESTRICTED_EXACT_REGIMES' if use_unrestricted else 'BOUNDED',
             'diagnostic_checkpoint': row_checkpoint,
             **artifact['size']}
        stages.append(row)
        if code in (cp_model.FEASIBLE,cp_model.OPTIMAL): solution=(artifact,solver,row); break
    selected=[]; material=[]; replay={'status':'NOT_REACHED'}; validator={'status':'NOT_REACHED'}
    event_violations=[]; isolation=[]
    objective_result={'status':'NOT_REQUESTED','stages':[],'before_metrics':None,'after_metrics':None}
    if solution:
        artifact, selected_solver, winning_stage = solution
        if objective_mode:
            try:
                objective_result=solve_lexicographic_objectives(
                    state, artifact, selected_solver, manager_overrides=manager_overrides,
                    time_limit_seconds=objective_time_limit_seconds,
                    deadline=attempt_deadline)
                selected_solver=objective_result['solver']
            except ManagerPriorityInputError as exc:
                objective_result={'status':'MANAGER_INPUT_INCOMPLETE','error':str(exc),'stages':[],
                                  'before_metrics':None,'after_metrics':None}
                solution=None
        if solution:
            mark=time.perf_counter(); modes,selected,material=_materialize(state,artifact,selected_solver); materialization_seconds=time.perf_counter()-mark
            solution=(artifact,selected_solver,winning_stage)
            if objective_result.get('status') not in {'NOT_REQUESTED', 'MANAGER_INPUT_INCOMPLETE'}:
                objective_result['after_metrics']=selected_objective_metrics(
                    state, objective_result['objective']['context'], selected)
        else:
            materialization_seconds=0.0
        if solution:
            mark=time.perf_counter(); replay,validator,event_violations,isolation=_accept(state,modes,selected,material); acceptance_seconds=time.perf_counter()-mark
        else:
            acceptance_seconds=0.0
    else:
        materialization_seconds=0.0; acceptance_seconds=0.0
    mismatch_count=sum(bool(x.get('mismatches')) for x in material)
    solved=bool(solution) and mismatch_count==0 and replay['status']=='PASS' and validator['status']=='VALID' and not event_violations and not isolation
    deadline_exhausted = bool(attempt_deadline is not None and time.perf_counter() >= attempt_deadline)
    if fast.enabled and deadline_exhausted:
        search_limit_status = ('SEARCH_LIMIT_WITH_VALID_INCUMBENT' if solved
                                else 'SEARCH_LIMIT_NO_INCUMBENT')
    else:
        search_limit_status = None
    prepared['acceptance']={'status':'ACCEPTED' if solved else 'REJECTED','stages':stages,
        'search_limit_status':search_limit_status,
        'attempt_deadline_exhausted':deadline_exhausted,
        'attempt_budget_seconds':attempt_budget if fast.enabled else None,
        'winning_stage':solution[2] if solution else None,'selected_schedule':selected,
        'materialization':{'mismatch_count':mismatch_count,'operations':material},
        'replay':replay,'validator':validator,'event_violations':event_violations,
        'factory_isolation':{'violation_count':len(isolation),'violations':isolation},
        'post_cp_modifications':0,'database_writes':0,
        'anchor_provenance':provenance,
        'objective':objective_result,
        'timing':{'materialization_seconds':materialization_seconds,
                  'replay_validator_seconds':acceptance_seconds,
                  'solve_acceptance_total_seconds':time.perf_counter()-started}}
    return prepared


# Compatibility name retained for existing repair callers.  New planning code
# imports ``run_canonical_schedule`` so it cannot imply disruption semantics.
solve_prepared_repair = run_canonical_schedule
