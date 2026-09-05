/**
 * Small inline icon shown in front of each ticket type.
 *
 * Clean line marks in the peacock palette — not emoji, not cartoon. Chosen by the
 * ticket type's segment code, with a neutral person mark for anything unrecognised,
 * so a venue that invents a new segment still renders sensibly.
 *
 * The icon is decorative reinforcement: the ticket type's name is always present as
 * text beside it, so meaning never depends on the glyph.
 */

type Props = { segment: string; className?: string };

function classify(segment: string): "ADULT" | "CHILD" | "SENIOR" | "DEFAULT" {
  const code = (segment || "").toUpperCase();
  if (code.includes("ADULT")) return "ADULT";
  if (code.includes("CHILD") || code.includes("KID")) return "CHILD";
  if (code.includes("SENIOR") || code.includes("ELDER")) return "SENIOR";
  return "DEFAULT";
}

export function SegmentIcon({ segment, className }: Props) {
  const kind = classify(segment);
  const common = {
    viewBox: "0 0 48 48",
    fill: "none" as const,
    stroke: "currentColor",
    strokeWidth: 2.2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    focusable: false,
    className: className ?? "h-6 w-6 text-primary-deep",
  };

  if (kind === "ADULT") {
    return (
      <svg {...common}>
        <circle cx="17" cy="12" r="5" />
        <path d="M17 18c-4 0-7 3-7 7v13h5v-9" />
        <path d="M17 29v9" />
        <circle cx="32" cy="12" r="5" />
        <path d="M32 18c-4 0-7 3-7 7 0 0 2 1 4 1l-1 12h4l1-8 1 8h4l-1-12c2 0 4-1 4-1 0-4-3-7-7-7" />
      </svg>
    );
  }
  if (kind === "CHILD") {
    return (
      <svg {...common}>
        <circle cx="24" cy="13" r="5" />
        <path d="M24 19c-4 0-6 3-6 6v7h3v9" />
        <path d="M24 19c4 0 6 3 6 6v7h-3v9" />
        <path d="M21 41h6" />
      </svg>
    );
  }
  if (kind === "SENIOR") {
    return (
      <svg {...common}>
        <circle cx="21" cy="12" r="5" />
        <path d="M21 18c-4 0-7 3-7 7v6h4l1 11h4" />
        <path d="M25 25l4 3" />
        <path d="M33 20v22" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="24" cy="13" r="5" />
      <path d="M24 19c-5 0-8 3-8 8v11h16V27c0-5-3-8-8-8z" />
    </svg>
  );
}
