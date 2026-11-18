import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"

export interface SelectOption {
  label: string
  value: string
}

/* 组合官方 radix Select 的窄封装：options 数组 + className，替代原生 <select> 用法 */
export function SimpleSelect({
  options,
  value,
  onValueChange,
  className,
}: {
  options: SelectOption[]
  value: string
  onValueChange: (value: string) => void
  className?: string
}) {
  return (
    <Select value={value} onValueChange={onValueChange}>
      <SelectTrigger className={cn("w-44", className)}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
