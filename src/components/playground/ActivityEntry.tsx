import { motion } from "framer-motion";
import type { ActivityEntry as ActivityEntryType, DetailLevel } from "@/types/activity";

interface Props {
  entry: ActivityEntryType;
  detailLevel: DetailLevel;
}

export function ActivityEntryItem({ entry }: Props) {
  const color =
    entry.status === "success"
      ? "text-[hsl(220,14%,62%)]"
      : entry.status === "failure"
      ? "text-[#ff453a]"
      : entry.status === "progress"
      ? "text-[#a1a1a6]"
      : "text-[#6e6e73]";

  const icon =
    entry.status === "success"
      ? "✓"
      : entry.status === "failure"
      ? "✗"
      : "›";

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.15 }}
      className="flex items-start gap-2.5 py-1 px-1"
    >
      <span className={`${color} text-[12px] shrink-0 mt-[1px] font-mono w-3 text-center`}>
        {icon}
      </span>
      <span className={`${color} text-[13px] leading-[1.5] font-sans`}>
        {entry.summary}
      </span>
    </motion.div>
  );
}
