import { CalendarDays } from "lucide-react";
import { Button, Input } from "../ui/primitives";
import { t } from "../i18n";
import { todayISO } from "../lib/format";

/** The first day of the current week (Monday) as ``YYYY-MM-DD``. */
export function weekStart(today = todayISO()): string {
  const day = new Date(`${today}T00:00:00Z`);
  const weekday = (day.getUTCDay() + 6) % 7; // Monday = 0
  day.setUTCDate(day.getUTCDate() - weekday);
  return day.toISOString().slice(0, 10);
}

/** The first day of the current month as ``YYYY-MM-DD``. */
export function monthStart(today = todayISO()): string {
  return `${today.slice(0, 7)}-01`;
}

/** RES-1: calendar input plus Today / This week / This month shortcuts and a custom range. */
export function DateSelector({
  date,
  onDate,
  range,
  onRange,
  withRange,
}: {
  date: string;
  onDate: (value: string) => void;
  range?: { from: string; to: string };
  onRange?: (value: { from: string; to: string }) => void;
  withRange: boolean;
}) {
  const today = todayISO();
  const useRange = withRange && Boolean(range && range.from && range.to);
  const inWeek = useRange && range?.from === weekStart(today) && range?.to === today;
  const inMonth = useRange && range?.from === monthStart(today) && range?.to === today;
  const pickRange = (from: string) => {
    if (withRange && onRange) {
      onRange({ from, to: today });
    } else {
      onDate(today);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <CalendarDays className="h-4 w-4 text-muted-foreground" />
      <Input
        type="date"
        className="h-8 w-[9.5rem] text-xs"
        value={date}
        onChange={(event) => onDate(event.target.value)}
      />
      <Button size="sm" variant={!useRange && date === today ? "primary" : "outline"} onClick={() => onDate(today)}>
        {t("common.today")}
      </Button>
      <Button size="sm" variant={inWeek ? "primary" : "outline"} onClick={() => pickRange(weekStart(today))}>
        {t("common.thisWeek")}
      </Button>
      <Button size="sm" variant={inMonth ? "primary" : "outline"} onClick={() => pickRange(monthStart(today))}>
        {t("common.thisMonth")}
      </Button>
      {withRange && range && onRange ? (
        <span className="flex items-center gap-1">
          <Input
            type="date"
            className="h-8 w-[9.5rem] text-xs"
            value={range.from}
            onChange={(event) => onRange({ ...range, from: event.target.value })}
            aria-label={t("common.from")}
          />
          <span className="text-xs text-muted-foreground">–</span>
          <Input
            type="date"
            className="h-8 w-[9.5rem] text-xs"
            value={range.to}
            onChange={(event) => onRange({ ...range, to: event.target.value })}
            aria-label={t("common.to")}
          />
          {useRange ? (
            <Button size="sm" variant="ghost" onClick={() => onRange({ from: "", to: "" })}>
              {t("common.clear")}
            </Button>
          ) : null}
        </span>
      ) : null}
    </div>
  );
}
