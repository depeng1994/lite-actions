export class PublicCache {
  private entries = new Map<string, {expires: number; json: string}>();

  get(key: string): string | undefined {
    const item = this.entries.get(key);
    if (item && item.expires > Date.now()) return item.json;
    this.entries.delete(key);
  }
  put(key: string, json: string) {
    if (json.length > 256 * 1024) return;
    if (this.entries.size >= 32) this.entries.delete(this.entries.keys().next().value!);
    this.entries.set(key, {expires: Date.now() + 5000, json});
  }
}
