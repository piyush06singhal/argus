/**
 * Checkout client (ARGUS demo).
 *
 * Mirrors the server-side call path so the TypeScript parser and the trace
 * mapper have a second language to reason about.
 */

export interface CheckoutRequest {
  sku: string;
  quantity: number;
}

export const CHECKOUT_TIMEOUT_MS = 1500;

export class CheckoutClient {
  constructor(private readonly baseUrl: string) {}

  async submit(request: CheckoutRequest): Promise<void> {
    await this.post("/checkout", request);
  }

  private async post(path: string, body: CheckoutRequest): Promise<void> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`checkout failed with ${response.status}`);
    }
  }
}
