-- Atomic check-and-reserve against a sliding-window rate limiter.
--
-- Two parallel sorted sets, one per metric:
--   tpm_key  ZSET, score = tokens reserved, member = "<now_ms>:<uuid>"
--   rpm_key  ZSET, score = 1,               member = "<now_ms>:<uuid>"
--
-- Encoding the timestamp in the member name (rather than the score)
-- decouples eviction from accounting:
--   - eviction:   parse the prefix, remove members older than now-60s
--   - accounting: sum the scores of the remaining members
--
-- This is the simplest scheme that gives both atomic operations
-- correctly under EVAL.
--
-- KEYS:
--   1) tpm_key
--   2) rpm_key
--
-- ARGV:
--   1) now_ms       current time in ms (caller-supplied for test
--                   determinism)
--   2) tpm_limit    max tokens-per-minute
--   3) rpm_limit    max requests-per-minute
--   4) tokens       estimated tokens for this request
--   5) member_uuid  unique id (uuid.hex) for this reservation
--
-- Returns one of:
--   {1, 0,                  current_tpm, current_rpm}  -- ALLOW
--   {0, retry_after_ms,     current_tpm, current_rpm}  -- DENY

local tpm_key       = KEYS[1]
local rpm_key       = KEYS[2]
local now_ms        = tonumber(ARGV[1])
local tpm_limit     = tonumber(ARGV[2])
local rpm_limit     = tonumber(ARGV[3])
local tokens        = tonumber(ARGV[4])
local member_uuid   = ARGV[5]

local window_ms = 60000
local cutoff = now_ms - window_ms

-- Walk each ZSET, parse the timestamp prefix from each member, and
-- remove anything older than the cutoff. Lua-side filtering rather
-- than ZREMRANGEBYSCORE because the score is tokens, not time.
local function evict_expired(key)
    local members = redis.call('ZRANGE', key, 0, -1)
    for _, m in ipairs(members) do
        local sep = string.find(m, ':')
        if sep then
            local ts = tonumber(string.sub(m, 1, sep - 1))
            if ts and ts < cutoff then
                redis.call('ZREM', key, m)
            end
        end
    end
end

evict_expired(tpm_key)
evict_expired(rpm_key)

-- Sum scores of remaining members.
local function sum_scores(key)
    local entries = redis.call('ZRANGE', key, 0, -1, 'WITHSCORES')
    local total = 0
    for i = 2, #entries, 2 do
        total = total + tonumber(entries[i])
    end
    return total
end

local current_tpm = sum_scores(tpm_key)
local current_rpm = redis.call('ZCARD', rpm_key)

if (current_tpm + tokens) > tpm_limit or (current_rpm + 1) > rpm_limit then
    -- Compute the earliest moment a slot frees up: the timestamp
    -- prefix of the oldest member, plus the window.
    local function oldest_ts(key)
        local first = redis.call('ZRANGE', key, 0, 0)
        if #first < 1 then return nil end
        local m = first[1]
        local sep = string.find(m, ':')
        if not sep then return nil end
        return tonumber(string.sub(m, 1, sep - 1))
    end

    local oldest = oldest_ts(tpm_key) or oldest_ts(rpm_key)
    local retry_after = window_ms
    if oldest then
        retry_after = math.max(0, (oldest + window_ms) - now_ms)
    end
    return {0, retry_after, current_tpm, current_rpm}
end

local member = tostring(now_ms) .. ':' .. member_uuid
redis.call('ZADD', tpm_key, tokens, member)
redis.call('ZADD', rpm_key, 1, member)

-- TTL safety net: idle keys clean up after 5x the window.
redis.call('PEXPIRE', tpm_key, window_ms * 5)
redis.call('PEXPIRE', rpm_key, window_ms * 5)

return {1, 0, current_tpm + tokens, current_rpm + 1}
