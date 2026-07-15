import { useRef, useState } from "react";

/**
 * Shared hook for managing open/closed state of reasoning/tool/observation cards.
 *
 * Root cause we are addressing:
 * - Card components previously stored their collapse state in local component
 *   state only. When the streaming pipeline re-materialised groups (e.g. when
 *   additional events arrived or history was hydrated), React could remount
 *   those components and re-apply the defaultOpen flag, effectively
 *   "forgetting" that the user had collapsed them.
 *
 * This hook keeps a small in-memory store keyed by a stable identifier
 * (e.g. group.key or message.id) so user-driven collapse/expand decisions
 * persist across remounts for the same logical card.
 */

const cardToggleState = new Map<string, boolean>();

export function useCardToggleState(
  stateKey: string | undefined,
  defaultOpen: boolean,
): [boolean, (next: boolean | ((prev: boolean) => boolean)) => void] {
  const keyRef = useRef(stateKey);

  const [isOpen, setIsOpen] = useState<boolean>(() => {
    if (!stateKey) return defaultOpen;
    if (cardToggleState.has(stateKey)) {
      return cardToggleState.get(stateKey)!;
    }
    cardToggleState.set(stateKey, defaultOpen);
    return defaultOpen;
  });

  const setAndPersist = (next: boolean | ((prev: boolean) => boolean)) => {
    setIsOpen((prev) => {
      const resolved = typeof next === "function" ? (next as (prev: boolean) => boolean)(prev) : next;
      const key = keyRef.current;
      if (key) {
        cardToggleState.set(key, resolved);
      }
      return resolved;
    });
  };

  return [isOpen, setAndPersist];
}


