import type { Metadata } from 'next';
import Link from 'next/link';
import './globals.css';
import Sidebar from './components/Sidebar';

export const metadata: Metadata = {
  title: {
    default: 'ARGUS Intelligence',
    template: '%s | ARGUS Intelligence',
  },
  description:
    'ARGUS Intelligence - Observability, Incident Response, and Deployment tracking for your infrastructure.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="flex min-h-screen">
        <Sidebar />
        <main className="flex flex-1 flex-col">
          <header className="flex h-16 shrink-0 items-center gap-4 border-b border-slate-800 bg-slate-900/40 px-6">
            <div className="flex items-center gap-3 text-sm text-slate-400">
              <Link href="/" className="font-medium text-argus-accent">
                ARGUS
              </Link>
              <span className="text-slate-600">/</span>
              <span>Observability &amp; Incident Response</span>
            </div>
          </header>
          <div className="flex-1 p-6">{children}</div>
        </main>
      </body>
    </html>
  );
}