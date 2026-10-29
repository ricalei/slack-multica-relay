import {
  serve,
  type VercelLikeRequest,
  type VercelLikeResponse,
} from "../../src/vercel.js";
import { ignoreIssue } from "../../src/ignore.js";

export const config = { api: { bodyParser: false } };

export default async function handler(
  request: VercelLikeRequest,
  response: VercelLikeResponse,
): Promise<void> {
  await serve(request, response, ignoreIssue);
}
