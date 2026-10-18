import { useEffect, useRef } from "react";

/** Trap focus while a sheet is open, support Escape, then restore its trigger. */
export function useDialog(onClose: () => void) {
 const ref=useRef<HTMLDivElement>(null);
 const close=useRef(onClose);
 useEffect(()=>{close.current=onClose;},[onClose]);
 useEffect(()=>{const previous=document.activeElement as HTMLElement|null;const node=ref.current;if(!node)return;
 const controls=()=>Array.from(node.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),[tabindex="0"]'));
 controls()[0]?.focus();
 function key(event:KeyboardEvent){if(event.key==="Escape"){event.preventDefault();close.current();}if(event.key==="Tab"){const items=controls(),first=items[0],last=items.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}}
 node.addEventListener("keydown",key);return()=>{node.removeEventListener("keydown",key);previous?.focus();};},[]);
 return ref;
}
