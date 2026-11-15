import { useEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "../../app/theme";
import { FactoryScene, lineColor, type LitMachine, type MachineState } from "./scene";
import { sceneQuality } from "./quality";

export interface SceneHall { key: string; name: string; note?: string; machines: { id: string; state: MachineState }[] }

const STATE_TEXT: Record<MachineState, string> = { run: "In use", fix: "Under maintenance", down: "Breakdown", idle: "Idle" };

/** The 3D factory with its line labels and tooltip. Three.js loads with this file, so pages only pay for it when they show it. */
export default function FloorScene({ halls, lit, belts, moving, selected, onSelect, compact, passive, onFail }: {
  halls: SceneHall[];
  lit?: Map<string, LitMachine>;
  belts?: Map<string, string[]>;
  moving?: boolean;
  selected?: string | null;
  onSelect?: (line: string | null) => void;
  compact?: boolean;
  passive?: boolean;
  onFail: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const scene = useRef<FactoryScene | null>(null);
  const tags = useRef(new Map<string, HTMLElement>());
  const select = useRef(onSelect); select.current = onSelect;
  const names = useRef(new Map<string, string>()); names.current = new Map(halls.map((h) => [h.key, h.name]));
  const theme = useTheme();
  const [tip, setTip] = useState<{ text: string; x: number; y: number } | null>(null);
  const key = useMemo(() => JSON.stringify(halls.map((h) => [h.key, h.machines])), [halls]);
  const labelled = !compact && !passive;

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    let made: FactoryScene;
    try {
      made = new FactoryScene(el, {
        lines: halls.map((h) => ({ key: h.key, machines: h.machines })), theme, compact, passive,
        quality: sceneQuality() === "high" ? "high" : "low", onTooSlow: onFail,
        onHover: (hit) => setTip(hit ? { x: hit.x, y: hit.y, text: `Machine ${hit.id}, ${names.current.get(hit.line) ?? hit.line}. ${hit.lit ? hit.lit.text : STATE_TEXT[hit.state]}.` } : null),
        onPick: (line) => select.current?.(line),
        onAnchors: labelled ? (anchors) => {
          const width = el.clientWidth, height = el.clientHeight;
          for (const [line, at] of Object.entries(anchors)) {
            const tag = tags.current.get(line);
            if (!tag) continue;
            const x = Math.max(10, Math.min(width - tag.offsetWidth - 10, at.x - tag.offsetWidth - 8));
            const y = Math.max(10, Math.min(height - tag.offsetHeight - 10, at.y - tag.offsetHeight / 2));
            tag.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
          }
        } : undefined,
      });
    } catch {
      onFail();
      return;
    }
    scene.current = made;
    return () => { made.dispose(); scene.current = null; };
    // The scene is rebuilt only when the machines change; theme and selection update it in place below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, compact, passive]);

  useEffect(() => { scene.current?.setTheme(theme); }, [theme]);
  useEffect(() => { scene.current?.setLit(lit ?? new Map(), belts ?? new Map()); }, [lit, belts, key, theme]);
  useEffect(() => { scene.current?.focus(selected ?? null); }, [selected, key]);
  useEffect(() => { scene.current?.setMoving(Boolean(moving)); }, [moving, key]);

  return (
    <div className={`scene${compact ? " compact" : ""}`} ref={box}>
      {labelled && halls.map((hall, i) => (
        <button key={hall.key} type="button" className={`scene-tag${selected && selected !== hall.key ? " dim" : ""}`} aria-pressed={selected === hall.key}
          style={{ "--tag-c": lineColor(i, theme) } as React.CSSProperties} onClick={() => onSelect?.(selected === hall.key ? null : hall.key)}
          ref={(node) => { if (node) tags.current.set(hall.key, node); else tags.current.delete(hall.key); }}>
          <b>{hall.name}</b>{hall.note && <span>{hall.note}</span>}
        </button>
      ))}
      {labelled && selected && <button type="button" className="scene-reset" onClick={() => onSelect?.(null)}>Show whole floor</button>}
      {labelled && <span className="scene-hint">Drag to look around</span>}
      {tip && <div className="scene-tip" role="tooltip" style={{ left: Math.min(window.innerWidth - 290, tip.x + 14), top: tip.y + 16 }}>{tip.text}</div>}
    </div>
  );
}
