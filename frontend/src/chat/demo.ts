// DEMO DATA: the stable ids `python manage.py seed_demo` creates (uuid5), so /chat/demo works with no setup.
import type { Group } from "./types";

export const DEMO_GROUP: Group = {
  id: "d779042e-0587-50ef-b462-9d798abff1e0",
  name: "Tahoe weekend",
  timezone: "America/Los_Angeles",
  members: [
    { id: "6e4752b1-d058-597e-b5ee-d6f240f5c511", display_name: "Maya", role: "organizer", status: "active" },
    { id: "60e32531-7d8a-5951-ac23-c17bb300e4e5", display_name: "Priya", role: "member", status: "active" },
    { id: "59ae96b4-6106-50a2-bbc5-6487853182e5", display_name: "Sam", role: "member", status: "active" },
    { id: "fc54f440-be43-5fb4-8520-2f87e4c84a8d", display_name: "Leo", role: "member", status: "active" },
    { id: "912b070a-70e1-57f4-8cc6-aa9495370362", display_name: "Jordan", role: "member", status: "active" },
    { id: "567927ac-d23c-5199-baf0-f43d89850e2f", display_name: "Alex", role: "member", status: "active" },
  ],
};

export function resolveGroupId(param: string | undefined): string {
  return !param || param === "demo" ? DEMO_GROUP.id : param;
}
