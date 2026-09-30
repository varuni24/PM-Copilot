import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PM Copilot",
  description: "Project intelligence across tasks, meetings, files, and team updates",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
