import "@testing-library/jest-dom/vitest";

class ResizeObserverMock {
  observe() { /* jsdom has no layout engine; the Recharts fixture only needs the observer contract. */ }
  unobserve() {}
  disconnect() {}
}
Object.defineProperty(globalThis, "ResizeObserver", { writable: true, value: ResizeObserverMock });
