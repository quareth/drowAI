/**
 * React hook that resolves visible app destinations and groups search matches.
 */
import { useMemo } from "react";

import { getAppSearchDestinations } from "@/navigation/registry";
import { groupSearchMatches, searchDestinations } from "@/navigation/search";
import type { SearchResultGroup } from "@/navigation/types";

export function useAppDestinationSearch(
  query: string,
  permissions: readonly string[] | undefined,
): SearchResultGroup[] {
  const permissionSet = useMemo(() => new Set(permissions ?? []), [permissions]);
  const destinations = useMemo(
    () => getAppSearchDestinations({ permissions: permissionSet }),
    [permissionSet],
  );

  return useMemo(
    () => groupSearchMatches(searchDestinations(destinations, query)),
    [destinations, query],
  );
}
