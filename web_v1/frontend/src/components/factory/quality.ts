/** How much 3D this computer should draw. */
export type SceneQuality = "high" | "low" | "none";

let cached: SceneQuality | null = null;

/**
 * Decided once per visit, without loading the 3D library:
 * - "none": no WebGL, or WebGL drawn on the processor. That can freeze a laptop, so the flat map shows instead.
 * - "low": a built-in graphics chip or few processor cores. No shadows, plain resolution, lower frame rate.
 * - "high": everything else.
 */
export function sceneQuality(): SceneQuality {
  if (cached) return cached;
  cached = detect();
  return cached;
}

function detect(): SceneQuality {
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    if (!gl) return "none";
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const name = String(info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER));
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    if (/swiftshader|llvmpipe|software|basic render/i.test(name)) return "none";
    const cores = navigator.hardwareConcurrency ?? 4;
    const memory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory ?? 8;
    if (/intel.*(hd|uhd)|mali|adreno|powervr/i.test(name) || cores <= 4 || memory <= 4) return "low";
    return "high";
  } catch {
    return "none";
  }
}
