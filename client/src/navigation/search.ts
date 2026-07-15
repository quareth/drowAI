/**
 * Pure search helpers for ranking and grouping app destinations.
 */
import type {
  SearchDestination,
  SearchDestinationGroup,
  SearchMatch,
  SearchResultGroup,
} from "@/navigation/types";

const MAX_RESULT_COUNT = 8;
const EXACT_MATCH_SCORE = 100;
const PREFIX_MATCH_SCORE = 70;
const WORD_PREFIX_MATCH_SCORE = 55;
const SUBSTRING_MATCH_SCORE = 35;
const KEYWORD_SCORE_OFFSET = 10;
const DESCRIPTION_SCORE_OFFSET = -10;

const GROUP_ORDER: readonly SearchDestinationGroup[] = [
  "Navigation",
  "Workspace",
  "Knowledge",
  "Settings",
  "Reports",
  "Profile",
];

function normalizeSearchText(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, " ");
}

function scoreField(query: string, value: string, offset = 0): number {
  const normalized = normalizeSearchText(value);
  if (!normalized) {
    return 0;
  }
  if (normalized === query) {
    return EXACT_MATCH_SCORE + offset;
  }
  if (normalized.startsWith(query)) {
    return PREFIX_MATCH_SCORE + offset;
  }
  if (normalized.split(" ").some((part) => part.startsWith(query))) {
    return WORD_PREFIX_MATCH_SCORE + offset;
  }
  if (normalized.includes(query)) {
    return SUBSTRING_MATCH_SCORE + offset;
  }
  return 0;
}

function scoreDestination(query: string, destination: SearchDestination): number {
  const scores = [
    scoreField(query, destination.label),
    scoreField(query, destination.description ?? "", DESCRIPTION_SCORE_OFFSET),
    ...destination.keywords.map((keyword) => scoreField(query, keyword, KEYWORD_SCORE_OFFSET)),
  ];
  return Math.max(...scores);
}

export function searchDestinations(
  destinations: readonly SearchDestination[],
  query: string,
): SearchMatch[] {
  const normalizedQuery = normalizeSearchText(query);
  if (!normalizedQuery) {
    return [];
  }

  return destinations
    .map((destination) => ({ destination, score: scoreDestination(normalizedQuery, destination) }))
    .filter((match) => match.score > 0)
    .sort((a, b) => {
      if (b.score !== a.score) {
        return b.score - a.score;
      }
      return a.destination.label.localeCompare(b.destination.label);
    })
    .slice(0, MAX_RESULT_COUNT);
}

export function groupSearchMatches(matches: readonly SearchMatch[]): SearchResultGroup[] {
  const matchesByGroup = new Map<SearchDestinationGroup, SearchMatch[]>();
  matches.forEach((match) => {
    const current = matchesByGroup.get(match.destination.group) ?? [];
    current.push(match);
    matchesByGroup.set(match.destination.group, current);
  });

  return GROUP_ORDER
    .filter((group) => matchesByGroup.has(group))
    .map((group) => ({ group, matches: matchesByGroup.get(group) ?? [] }));
}
