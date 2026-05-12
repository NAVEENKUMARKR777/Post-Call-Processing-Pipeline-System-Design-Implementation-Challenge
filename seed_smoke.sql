INSERT INTO leads (id, customer_id, campaign_id, phone, name, stage)
VALUES (
  'a0000000-0000-0000-0000-000000000001',
  'd0000000-0000-0000-0000-000000000001',
  'c0000000-0000-0000-0000-000000000001',
  '+919900000001', 'Test Lead', 'new'
) ON CONFLICT (id) DO NOTHING;

INSERT INTO sessions (id, lead_id, campaign_id, customer_id, agent_id, status)
VALUES (
  '1fd667e0-676d-4330-97e4-fc7b48978c3c',
  'a0000000-0000-0000-0000-000000000001',
  'c0000000-0000-0000-0000-000000000001',
  'd0000000-0000-0000-0000-000000000001',
  'e0000000-0000-0000-0000-000000000001',
  'ACTIVE'
) ON CONFLICT (id) DO NOTHING;

INSERT INTO interactions (id, session_id, lead_id, campaign_id, customer_id, agent_id, status, conversation_data)
VALUES (
  'ff000000-0000-0000-0000-000000000006',
  '1fd667e0-676d-4330-97e4-fc7b48978c3c',
  'a0000000-0000-0000-0000-000000000001',
  'c0000000-0000-0000-0000-000000000001',
  'd0000000-0000-0000-0000-000000000001',
  'e0000000-0000-0000-0000-000000000001',
  'IN_PROGRESS',
  '{"transcript":[{"role":"agent","content":"Hello, am I speaking with Mr. Kumar?"},{"role":"customer","content":"Yes, speaking."},{"role":"agent","content":"Calling about your product inquiry."},{"role":"customer","content":"Yes I was interested."},{"role":"agent","content":"Would you like to schedule a demo?"},{"role":"customer","content":"Yes, Thursday confirmed."}]}'
) ON CONFLICT (id) DO NOTHING;