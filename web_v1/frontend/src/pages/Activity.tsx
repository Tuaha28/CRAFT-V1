import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { eventText, when } from "../lib/format";
import { Loading, Notice, PageHead } from "../components/ui";

export default function Activity() {
  const events = useQuery({ queryKey: ["events"], queryFn: api.events, refetchInterval: 15_000 });
  if (events.isPending) return <Loading />;
  if (events.isError) return <Notice tone="error">{events.error.message}</Notice>;
  return (
    <>
      <PageHead title="Activity" lead="Who requested, cancelled, approved or rejected which plan, and when." />
      <section className="panel">
        {!events.data.length ? <p className="muted">Nothing has happened yet.</p> : (
          <ol className="timeline">{events.data.map((e, i) => (
            <li key={i}><time>{when(e.created_at)}</time>
              <span>{eventText(e.message)}{e.actor ? ` (${e.actor})` : ""}{e.run_id && <Link to={`/runs/${e.run_id}`} style={{ marginLeft: 10 }}>Open plan</Link>}</span></li>))}
          </ol>)}
      </section>
    </>
  );
}
