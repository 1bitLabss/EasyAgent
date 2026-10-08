import { useEffect, useRef, useSyncExternalStore } from "react";
import { faceMood, subscribeMood, type Mood } from "@/lib/mood";
import { cn } from "@/lib/utils";
import type { FaceName } from "@/lib/run";

export const FACE_PALETTE = [
  "#c4532a",
  "#2a6fdb",
  "#1f8a4c",
  "#c43b7a",
  "#b86e12",
  "#5c4d9a",
  "#0e7c86",
  "#8f2d28",
  "#3d6b4f",
  "#a34b2e",
  "#3a4f8a",
  "#6b4a2a",
];

const FRAMES = ["eyes-mid", "eyes-left", "eyes-right", "eyes-up", "eyes-squint", "eyes-x", "mouth-smile", "mouth-flat", "mouth-open"];

let template: SVGSVGElement | null = null;
let loading: Promise<SVGSVGElement | null> | null = null;

function markupOk(svg: Element) {
  return FRAMES.every((name) => svg.querySelector("." + name));
}

export function loadFace(): Promise<SVGSVGElement | null> {
  if (template) return Promise.resolve(template);
  if (!loading) {
    loading = fetch("/static/face.svg")
      .then(async (response) => {
        if (!response.ok) return null;
        const doc = new DOMParser().parseFromString(await response.text(), "image/svg+xml");
        const root = doc.documentElement;
        if (root.localName === "svg" && markupOk(root)) {
          template = root as unknown as SVGSVGElement;
          return template;
        }
        return null;
      })
      .catch(() => null);
  }
  return loading;
}

const STATES: FaceName[] = ["idle", "waiting", "reconnecting", "thinking", "tool", "talking", "halted"];

export function applyFaceState(node: HTMLElement, name: FaceName) {
  node.classList.toggle("bot-live", name !== "idle" && name !== "halted");
  if (node.dataset.faceState === name) return;
  node.dataset.faceState = name;
  for (const state of STATES) node.classList.remove(`is-${state}`);
  node.classList.add(`is-${name}`);
}

export function Face({
  color,
  state = "idle",
  botId = "",
  large = false,
  tiny = false,
  tile = false,
  selected = false,
}: {
  color?: string;
  state?: FaceName;
  botId?: string;
  large?: boolean;
  tiny?: boolean;
  tile?: boolean;
  selected?: boolean;
}) {
  const mood = useSyncExternalStore<Mood | null>(
    subscribeMood,
    () => (botId ? faceMood(botId) : null),
    () => null,
  );
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    let gone = false;
    void loadFace().then((svg) => {
      if (gone || !svg || !ref.current) return;
      const face = svg.cloneNode(true) as SVGSVGElement;
      // Screen only. Rows 0-8 are the antenna and rows 29-33 are the 101.
      // This window is the eyes and the smile, and it fills the tile.
      face.setAttribute("viewBox", "6 14 14 14");
      face.setAttribute("preserveAspectRatio", "xMidYMid meet");
      face.setAttribute("overflow", "hidden");
      ref.current.replaceChildren(face);
    });
    return () => {
      gone = true;
    };
  }, []);
  useEffect(() => {
    if (ref.current) applyFaceState(ref.current, state);
  }, [state]);
  return (
    <span
      ref={ref}
      className={cn(
        "buddy-face",
        `is-${state}`,
        large && "is-large",
        tiny && "is-tiny",
        tile && "is-tile",
        selected && "is-selected",
        mood === "glad" && "is-glad",
        mood === "sad" && "is-sad",
      )}
      style={{ ["--face" as string]: color || "#5c4d9a" }}
      aria-hidden
    />
  );
}
