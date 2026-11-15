import { createContext, useContext } from "react";
import type { Session } from "../lib/api";

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside an authenticated session");
  return session;
}

/** True when the signed-in user holds a permission such as "floor.enter_output". */
export function can(session: Session, perm: string): boolean {
  return session.perms?.includes(perm) ?? false;
}
