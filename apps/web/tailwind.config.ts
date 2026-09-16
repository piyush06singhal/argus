import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.tsx'],
  theme: {
    extend: {
      colors: {
        argus: {
          // Slate-based dark theme accent colors for the ARGUS Intelligence brand
          dark: {
            DEFAULT: '#020617', // slate-950
            surface: '#0f172a', // slate-900
            elevated: '#1e293b', // slate-800
            border: '#334155', // slate-700
          },
          accent: {
            DEFAULT: '#38bdf8', // sky-400
            hover: '#0284c7', // sky-600
            subtle: '#38bdf8',
          },
          success: '#34d399', // emerald-400
          warning: '#fbbf24', // amber-400
          error: '#f87171', // red-400
          info: '#818cf8', // indigo-400
        },
      },
      fontFamily: {
        sans: [
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Monaco',
          'Consolas',
          'Liberation Mono',
          'Courier New',
          'monospace',
        ],
      },
    },
  },
  plugins: [],
};

export default config;