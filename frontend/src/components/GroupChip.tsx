import type { GroupRef } from "../types";

/** PRO-11: a profile group's coloured label. */
export function GroupChip({ group }: { group: GroupRef }) {
  return (
    <span
      className="inline-flex max-w-[12rem] items-center truncate rounded-full border px-2 py-0.5 text-xs font-medium"
      style={{ borderColor: group.color, color: group.color, backgroundColor: `${group.color}1a` }}
      title={group.name}
    >
      {group.name}
    </span>
  );
}
