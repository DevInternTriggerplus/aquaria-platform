import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Book your visit | Aquaria",
  description:
    "Choose your date and tickets, pay securely and get a QR e-ticket by email. No account needed.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* Keyboard and screen-reader users get a way past the header. */}
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-0 focus:top-0 focus:z-50 focus:bg-primary-deep focus:px-4 focus:py-3 focus:text-primary-foreground"
        >
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
