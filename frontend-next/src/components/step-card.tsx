/** A numbered step card: the circular number sits inline with the heading. */

import type { ReactNode } from "react";

export function StepCard({
  step,
  title,
  children,
  aside,
}: {
  step: number;
  title: string;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-card)] border bg-card p-5 shadow-sm sm:p-6">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 className="flex items-center gap-2.5 text-xl font-semibold">
          <span
            className="flex h-6.5 w-6.5 flex-none items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground"
            style={{ height: "1.625rem", width: "1.625rem", fontFamily: "var(--font-sans)" }}
          >
            {step}
          </span>
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}
