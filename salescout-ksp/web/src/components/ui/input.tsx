import * as React from "react"

import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      className={cn(
        "flex h-10 w-full rounded-md border border-line bg-card px-3 text-sm text-ink outline-none placeholder:text-muted focus-visible:ring-2 focus-visible:ring-accent/30",
        className,
      )}
      {...props}
    />
  )
}

export { Input }
