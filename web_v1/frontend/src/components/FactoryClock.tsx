import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clock3, Pause, Play } from "lucide-react";
import { sim, type ClockState } from "../lib/api";
import { useScope } from "../app/scope";
import { ConfirmButton } from "./ui";

const SPEEDS = [1, 10, 60] as const;

function when(iso: string) {
  return new Date(iso).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
}

/** The simulated factory clock: where the factory is in time, and (for managers and admins) pause and speed. */
export function FactoryClock() {
  const { dataset, factoryId } = useScope();
  const client = useQueryClient();
  const d = dataset?.id, f = factoryId;
  const clock = useQuery({ queryKey: ["sim-clock", d, f], queryFn: () => sim.clock(d!, f!), enabled: Boolean(d && f), refetchInterval: 5000 });
  const control = useMutation({
    mutationFn: (body: { action: "start" | "speed" | "pause" | "reset"; speed?: number }) => sim.control(d!, f!, body),
    onSuccess: (data: ClockState) => {
      client.setQueryData(["sim-clock", d, f], data);
      return client.invalidateQueries();  // floor pages and progress follow the new factory time
    },
  });
  const c = clock.data;
  if (!c || (!c.now && !c.can_control)) return null;
  if (!c.now) {
    return (
      <div className="factory-clock idle">
        <button type="button" className="btn" disabled={control.isPending || !c.has_plan} title={c.has_plan ? "" : "Approve a plan for this factory first"}
          onClick={() => control.mutate({ action: "start", speed: 1 })}><Play size={15} aria-hidden="true" /> Run the factory</button>
      </div>
    );
  }
  return (
    <div className={`factory-clock${c.running ? " running" : ""}`} role="group" aria-label="Simulated factory clock">
      <span className="sim-badge"><Clock3 size={14} aria-hidden="true" />Simulated factory clock</span>
      <strong className="sim-time">{when(c.now)}</strong>
      {c.can_control && (
        <span className="sim-controls">
          <button type="button" className={`btn${c.speed === 0 ? " on" : ""}`} aria-pressed={c.speed === 0} disabled={control.isPending}
            onClick={() => control.mutate({ action: "pause" })}><Pause size={14} aria-hidden="true" /> Pause</button>
          {SPEEDS.map((s) => (
            <button key={s} type="button" className={`btn${c.speed === s ? " on" : ""}`} aria-pressed={c.speed === s} disabled={control.isPending}
              onClick={() => control.mutate({ action: c.now ? "speed" : "start", speed: s })}>{s}×</button>))}
          <ConfirmButton className="btn" label="Reset" disabled={control.isPending}
            confirm="Reset the simulated factory? The clock goes back to the start and every simulated output, check and problem for this factory is removed."
            onConfirm={() => control.mutate({ action: "reset" })} />
        </span>)}
    </div>
  );
}
