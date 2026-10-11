import { createContext, useContext, useState, type ReactNode } from "react";

const STORAGE_KEY = "chronos.advancedMode";

interface AdvancedModeValue {
  advanced: boolean;
  setAdvanced: (value: boolean) => void;
}

const AdvancedModeContext = createContext<AdvancedModeValue>({
  advanced: false,
  setAdvanced: () => {},
});

/**
 * Global Simple/Advanced toggle. Simple (the default for every new
 * session) never shows task_type strings, dependency ids, raw state
 * enums, retry/lease internals, or JSON -- only plain-language labels,
 * icons, and friendly status. Advanced reveals the existing
 * developer-oriented detail everywhere it already existed; nothing is
 * removed, only re-gated behind this one switch so it isn't the default
 * experience.
 */
export function AdvancedModeProvider({ children }: { children: ReactNode }) {
  const [advanced, setAdvancedState] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });

  function setAdvanced(value: boolean) {
    setAdvancedState(value);
    try {
      localStorage.setItem(STORAGE_KEY, value ? "1" : "0");
    } catch {
      // localStorage unavailable (private browsing, etc.) -- the toggle
      // still works for this session, it just won't persist.
    }
  }

  return (
    <AdvancedModeContext.Provider value={{ advanced, setAdvanced }}>
      {children}
    </AdvancedModeContext.Provider>
  );
}

export function useAdvancedMode(): AdvancedModeValue {
  return useContext(AdvancedModeContext);
}

export function AdvancedModeToggle() {
  const { advanced, setAdvanced } = useAdvancedMode();
  return (
    <label className="switch" title="Show technical details (retries, raw status, dependency ids, JSON)">
      <input
        type="checkbox"
        checked={advanced}
        onChange={(e) => setAdvanced(e.target.checked)}
      />
      <span className="switch-track" aria-hidden="true">
        <span className="switch-thumb" />
      </span>
      <span>Advanced</span>
    </label>
  );
}
