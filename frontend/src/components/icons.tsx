/**
 * A small, self-contained stroke-icon set (Lucide-style geometry,
 * hand-authored so Chronos pulls in no icon dependency). Every icon is a
 * 24×24 `currentColor` stroke glyph, so it inherits size from the CSS box
 * it sits in (`.btn svg`, `.badge svg`, etc.) and color from its parent —
 * that's what keeps iconography consistent across the whole product.
 *
 * Task-*type* glyphs (🔔 ⏱️ 🌐 🧮) still come from the backend's
 * TASK_TYPE_METADATA (GET /task-types) — those are intentional, friendly
 * category emoji owned by the server, not chrome; this set covers the UI
 * chrome (nav, actions, status) only.
 */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Svg({ children, ...props }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export const HomeIcon = (p: IconProps) => (
  <Svg {...p}><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9.5V20a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9.5" /><path d="M9.5 21v-6h5v6" /></Svg>
);

export const LayersIcon = (p: IconProps) => (
  <Svg {...p}><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 13 9 5 9-5" /></Svg>
);

export const PlayIcon = (p: IconProps) => (
  <Svg {...p}><path d="M7 4.5v15l12-7.5-12-7.5Z" fill="currentColor" stroke="none" /></Svg>
);

export const PlusIcon = (p: IconProps) => (
  <Svg {...p}><path d="M12 5v14M5 12h14" /></Svg>
);

export const RefreshIcon = (p: IconProps) => (
  <Svg {...p}><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v5h-5" /></Svg>
);

export const CheckIcon = (p: IconProps) => (
  <Svg {...p}><path d="m5 12.5 4.5 4.5L19 7" /></Svg>
);

export const CheckCircleIcon = (p: IconProps) => (
  <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="m8.5 12 2.5 2.5 4.5-5" /></Svg>
);

export const XIcon = (p: IconProps) => (
  <Svg {...p}><path d="M6 6l12 12M18 6 6 18" /></Svg>
);

export const XCircleIcon = (p: IconProps) => (
  <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="m9 9 6 6M15 9l-6 6" /></Svg>
);

export const AlertIcon = (p: IconProps) => (
  <Svg {...p}><path d="M12 3.5 22 20H2L12 3.5Z" /><path d="M12 10v4" /><circle cx="12" cy="17.2" r="0.4" fill="currentColor" /></Svg>
);

export const ClockIcon = (p: IconProps) => (
  <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7.5V12l3 2" /></Svg>
);

export const SkipIcon = (p: IconProps) => (
  <Svg {...p}><path d="M6 4v16M6 12h13" /><path d="m14 7 5 5-5 5" /></Svg>
);

export const ChevronRightIcon = (p: IconProps) => (
  <Svg {...p}><path d="m9 6 6 6-6 6" /></Svg>
);

export const ArrowLeftIcon = (p: IconProps) => (
  <Svg {...p}><path d="M19 12H5" /><path d="m11 6-6 6 6 6" /></Svg>
);

export const SearchIcon = (p: IconProps) => (
  <Svg {...p}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.2-3.2" /></Svg>
);

export const TrashIcon = (p: IconProps) => (
  <Svg {...p}><path d="M4 7h16" /><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" /><path d="M6 7v13a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7" /><path d="M10 11v6M14 11v6" /></Svg>
);

export const SlidersIcon = (p: IconProps) => (
  <Svg {...p}><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M18 18h2" /><circle cx="16" cy="6" r="2" /><circle cx="10" cy="12" r="2" /><circle cx="16" cy="18" r="2" /></Svg>
);

export const LogOutIcon = (p: IconProps) => (
  <Svg {...p}><path d="M9 21H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></Svg>
);

export const SaveIcon = (p: IconProps) => (
  <Svg {...p}><path d="M5 4h11l3 3v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z" /><path d="M8 4v5h7" /><path d="M8 15h8" /></Svg>
);

export const PencilIcon = (p: IconProps) => (
  <Svg {...p}><path d="M15.5 5.5 18.5 8.5" /><path d="M4 20l4-1 10.5-10.5a1.4 1.4 0 0 0 0-2l-1-1a1.4 1.4 0 0 0-2 0L5 16l-1 4Z" /></Svg>
);

export const BranchIcon = (p: IconProps) => (
  <Svg {...p}><circle cx="6" cy="6" r="2.5" /><circle cx="6" cy="18" r="2.5" /><circle cx="18" cy="9" r="2.5" /><path d="M6 8.5v7" /><path d="M18 11.5c0 3-3 3.5-6 3.5" /><path d="M15.8 9H8.5" /></Svg>
);

export const SparkIcon = (p: IconProps) => (
  <Svg {...p}><path d="M12 2.5c.3 4.2 1.6 5.5 5.8 5.8-4.2.3-5.5 1.6-5.8 5.8-.3-4.2-1.6-5.5-5.8-5.8 4.2-.3 5.5-1.6 5.8-5.8Z" fill="currentColor" stroke="none" /><path d="M18.5 14.5c.15 2.1.8 2.75 2.9 2.9-2.1.15-2.75.8-2.9 2.9-.15-2.1-.8-2.75-2.9-2.9 2.1-.15 2.75-.8 2.9-2.9Z" fill="currentColor" stroke="none" /></Svg>
);

export const InboxIcon = (p: IconProps) => (
  <Svg {...p}><path d="M4 13.5 6.5 6a1 1 0 0 1 1-.7h9a1 1 0 0 1 1 .7L20 13.5V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-5.5Z" /><path d="M4 13.5h4l1.5 2.5h5L16 13.5h4" /></Svg>
);

export const DotIcon = (p: IconProps) => (
  <Svg {...p}><circle cx="12" cy="12" r="4" fill="currentColor" stroke="none" /></Svg>
);

export const MaximizeIcon = (p: IconProps) => (
  <Svg {...p}><path d="M4 9V5a1 1 0 0 1 1-1h4M20 9V5a1 1 0 0 0-1-1h-4M4 15v4a1 1 0 0 0 1 1h4M20 15v4a1 1 0 0 1-1 1h-4" /></Svg>
);

export const SunIcon = (p: IconProps) => (
  <Svg {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2.5M12 19.5V22M4.2 4.2l1.8 1.8M18 18l1.8 1.8M2 12h2.5M19.5 12H22M4.2 19.8 6 18M18 6l1.8-1.8" /></Svg>
);

export const MoonIcon = (p: IconProps) => (
  <Svg {...p}><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z" /></Svg>
);

/* — Pipeline stage glyphs (used by the live orchestration hero) — */

export const ServerIcon = (p: IconProps) => (
  <Svg {...p}><rect x="3" y="4" width="18" height="7" rx="1.5" /><rect x="3" y="13" width="18" height="7" rx="1.5" /><path d="M7 7.5h.01M7 16.5h.01" /></Svg>
);

export const DatabaseIcon = (p: IconProps) => (
  <Svg {...p}><ellipse cx="12" cy="5.5" rx="8" ry="3" /><path d="M4 5.5v13c0 1.66 3.58 3 8 3s8-1.34 8-3v-13" /><path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3" /></Svg>
);

export const SendIcon = (p: IconProps) => (
  <Svg {...p}><path d="M21 3 10.5 13.5" /><path d="M21 3 14.5 21l-4-8-8-4L21 3Z" /></Svg>
);

export const CpuIcon = (p: IconProps) => (
  <Svg {...p}><rect x="7" y="7" width="10" height="10" rx="1.5" /><path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3" /></Svg>
);

export const FlagIcon = (p: IconProps) => (
  <Svg {...p}><path d="M5 21V4M5 4h12l-2 4 2 4H5" /></Svg>
);
