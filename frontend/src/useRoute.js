import { useCallback, useEffect, useState } from "react";
import { parseRoute, routeHref } from "./routes.js";

export default function useRoute() {
  const [view, setView] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const sync = () => setView(parseRoute(window.location.hash));
    window.addEventListener("popstate", sync);
    window.addEventListener("hashchange", sync);
    return () => {
      window.removeEventListener("popstate", sync);
      window.removeEventListener("hashchange", sync);
    };
  }, []);
  const go = useCallback((next, { replace = false } = {}) => {
    const href = routeHref(next);
    if (window.location.hash !== href) {
      window.history[replace ? "replaceState" : "pushState"](null, "", href);
    }
    setView(next);
  }, []);
  const context = useCallback((extra) => setView((v) => ({ ...v, ...extra })), []);
  return { view, go, context };
}
