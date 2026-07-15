/**
 * Navbar notification menu for recent task-scoped application events.
 *
 * Responsibilities:
 * - Render unread counts on the bell.
 * - Show recent unread notifications in a compact dropdown.
 * - Mark notifications read when the user closes or opens an item.
 */
import { Bell, Box, Bug, CheckCheck } from "lucide-react";
import { useLocation } from "wouter";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  markAllNotificationsRead,
  markNotificationRead,
  useNotificationSnapshot,
  type AppNotification,
} from "@/state/notification-store";
import { cn } from "@/lib/utils";

function notificationSourceLabel(item: AppNotification): string {
  const taskLabel = `Task #${item.taskId}`;
  const toolName = typeof item.metadata?.toolName === "string" ? item.metadata.toolName : null;
  return toolName ? `${taskLabel} · ${toolName}` : taskLabel;
}

function notificationTargetPath(item: AppNotification): string {
  if (item.category === "knowledge_delta") {
    return "/knowledge";
  }
  return "/";
}

function NotificationIcon({ item }: { item: AppNotification }) {
  if (item.category === "knowledge_delta") {
    const findingCount =
      typeof item.metadata?.findingCount === "number" ? item.metadata.findingCount : 0;
    return findingCount > 0 ? <Bug className="h-4 w-4" /> : <Box className="h-4 w-4" />;
  }
  return <Bell className="h-4 w-4" />;
}

export function NotificationMenu() {
  const [, setLocation] = useLocation();
  const { notifications, unreadCount } = useNotificationSnapshot();
  const hasUnread = unreadCount > 0;

  const handleOpenChange = (open: boolean) => {
    if (!open) {
      markAllNotificationsRead();
    }
  };

  const openNotification = (item: AppNotification) => {
    markNotificationRead(item.id);
    setLocation(notificationTargetPath(item));
  };

  return (
    <DropdownMenu onOpenChange={handleOpenChange}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          aria-label={hasUnread ? `${unreadCount} unread notifications` : "Notifications"}
          className={cn(
            "relative p-2 text-gray-400 hover:text-white",
            hasUnread && "text-sky-200 hover:text-sky-100",
          )}
        >
          <Bell className="w-4 h-4" />
          {hasUnread ? (
            <Badge className="absolute -right-1 -top-1 h-4 min-w-4 rounded-full border-0 bg-sky-500 px-1 text-[10px] leading-4 text-white">
              {unreadCount > 9 ? "9+" : unreadCount}
            </Badge>
          ) : null}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <div className="flex items-center justify-between px-2 py-1">
          <DropdownMenuLabel className="px-0 py-0 text-slate-200">Notifications</DropdownMenuLabel>
          {hasUnread ? (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-xs text-slate-400 hover:text-slate-100"
              onClick={markAllNotificationsRead}
            >
              <CheckCheck className="mr-1 h-3.5 w-3.5" />
              Mark read
            </Button>
          ) : null}
        </div>
        <DropdownMenuSeparator />
        {notifications.length === 0 ? (
          <div className="px-3 py-5 text-center text-sm text-slate-400">
            No new notifications
          </div>
        ) : (
          notifications.map((item) => (
            <DropdownMenuItem
              key={item.id}
              className="cursor-pointer items-start gap-3 px-3 py-2"
              onClick={() => openNotification(item)}
            >
              <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-slate-800 text-sky-300">
                <NotificationIcon item={item} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-medium text-slate-100">{item.title}</span>
                <span className="block text-xs text-slate-300">{item.body}</span>
                <span className="block truncate text-xs text-slate-500">{notificationSourceLabel(item)}</span>
              </span>
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
