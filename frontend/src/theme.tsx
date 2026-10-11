import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { SunIcon, MoonIcon } from "./components/icons";

const STORAGE_KEY = "chronos.theme";
type Theme = "dark" | "light";
const DEFAULT_THEME: Theme = "light";

interface ThemeValue {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeValue>({ theme: DEFAULT_THEME, setTheme: () => {}, toggle: () => {} });

function readInitial(): Theme {
  try {
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr === "light" || attr === "dark") return attr;
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // ignore
  }
  return DEFAULT_THEME;
}

/**
 * Light/dark theming. The whole design is token-driven, so a theme is just
 * a `data-theme` attribute on <html> that swaps the colour tokens (see
 * index.css). The initial value is applied by an inline script in
 * index.html before first paint (no flash); this provider keeps React in
 * sync and persists the choice.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readInitial);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // localStorage unavailable — the toggle still works for this session
    }
  }, [theme]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const toggle = useCallback(() => setThemeState((t) => (t === "dark" ? "light" : "dark")), []);

  return <ThemeContext.Provider value={{ theme, setTheme, toggle }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeValue {
  return useContext(ThemeContext);
}

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="btn ghost icon sm"
      onClick={toggle}
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode`}
    >
      {theme === "dark" ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}
