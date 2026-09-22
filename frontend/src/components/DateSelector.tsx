import { CalendarDays } from "lucide-react";
import { Button, Input } from "../ui/primitives";
import { t } from "../i18n";
import { shiftDate, todayISO } from "../lib/format";

/** RES-1: calendar input plus Today / Yesterday shortcuts; managers also get a range. */
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
  return (
    <div className="flex flex-wrap items-center gap-2">
      <CalendarDays className="h-4 w-4 text-muted-foreground" />
      <Input
        type="date"
        className="h-8 w-[9.5rem] text-xs"
        value={date}
        onChange={(event) => onDate(event.target.value)}
      />
      <Button size="sm" variant={date === today ? "primary" : "outline"} onClick={() => onDate(today)}>
        {t("common.today")}
      </Button>
      <Button size="sm" variant={date === shiftDate(today, -1) ? "primary" : "outline"} onClick={() => onDate(shiftDate(today, -1))}>
        {t("common.yesterday")}
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
