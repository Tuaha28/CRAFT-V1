import { useEffect, useRef, useState } from "react";

const reduced = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/**
 * A whole number that counts up from 0 when it first appears, and counts to the
 * new value with one flash when it changes later (new data from the floor).
 * Screen readers get the final value only.
 */
export function CountUp({ value, ms = 900 }: { value: number; ms?: number }) {
  const start = reduced() ? value : 0;
  const [shown, setShown] = useState(start);
  const [flash, setFlash] = useState(false);
  const current = useRef(start);   // the number on screen, also mid-animation
  const settled = useRef(false);   // true once the first count has finished

  useEffect(() => {
    const from = current.current;
    if (from === value || reduced()) {
      current.current = value;
      setShown(value);
      settled.current = true;
      return;
    }
    let flashTimer = 0;
    if (settled.current) {
      setFlash(true);
      flashTimer = window.setTimeout(() => setFlash(false), 1200);
    }
    let frame = 0;
    const t0 = performance.now();
    const step = (now: number) => {
      const p = Math.min(1, (now - t0) / ms);
      current.current = Math.round(from + (value - from) * (1 - Math.pow(1 - p, 3)));
      setShown(current.current);
      if (p < 1) frame = requestAnimationFrame(step);
      else settled.current = true;
    };
    frame = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(frame); window.clearTimeout(flashTimer); };
  }, [value, ms]);

  return (
    <span className={flash ? "count flash" : "count"}>
      <span aria-hidden="true">{shown.toLocaleString()}</span>
      <span className="sr-only">{value.toLocaleString()}</span>
    </span>
  );
}
