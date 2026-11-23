import config from "../config/model_repos.json";
import type { Provider } from "./types/api";

export const modelMap = config;

export function logicalModel(provider: Provider, repo: string): string {
  return config.models.find(model => model.repos.some(
    candidate => candidate.provider === provider && candidate.repo.toLowerCase() === repo.toLowerCase(),
  ))?.slug ?? repo.split("/")[1].toLowerCase();
}
