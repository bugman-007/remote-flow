import en, { type Dict } from "./en";

type Vars = Record<string, string | number>;

const dictionaries: Record<string, Dict> = { en };
let locale = (typeof navigator !== "undefined" && navigator.language?.startsWith("en") ? "en" : "en") as string;

export function setLocale(next: string): void {
  if (dictionaries[next]) locale = next;
}

export function getLocale(): string {
  return locale;
}

function lookup(dict: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((node, part) => {
    if (node && typeof node === "object" && part in (node as Record<string, unknown>)) {
      return (node as Record<string, unknown>)[part];
    }
    return undefined;
  }, dict);
}

export function t(path: string, vars?: Vars): string {
  const raw = lookup(dictionaries[locale], path) ?? lookup(dictionaries.en, path);
  if (typeof raw !== "string") return path;
  if (!vars) return raw;
  return raw.replace(/\{(\w+)\}/g, (_match, key: string) => String(vars[key] ?? `{${key}}`));
}

export const availableLocales = Object.keys(dictionaries);
