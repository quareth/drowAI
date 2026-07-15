# DrowAI FastAPI Backend

## Overview
This FastAPI backend replaces the Node.js/Express server with Python-based implementation using:
- **FastAPI** (Python 3.10+) - Modern, fast web framework
- **SQLAlchemy** (async ORM) - Database operations
- **PostgreSQL** - Database backend with asyncpg driver
- **JWT** with python-jose - Authentication and authorization
- **bcrypt** via passlib - Password hashing

## Architecture

### Core Components
- `main.py` - FastAPI application entry point
- `database.py` - SQLAlchemy async configuration
- `models.py` - Database models and Pydantic schemas
- `auth.py` - JWT authentication and password hashing
- `pentest_engine.py` - Pentesting simulation engine

### API Routers
- `routers/auth.py` - Authentication endpoints
- `routers/tasks.py` - Task management
- `routers/pentest.py` - Pentesting operations
- `routers/reports.py` - Report generation

## API Endpoints

### Authentication (`/api/auth`)
- `POST /register` - User registration
- `POST /login` - User authentication
- `GET /me` - Current user profile
- `POST /logout` - User logout
- `POST /refresh` - Token refresh
- `POST /change-password` - Password change

### Tasks (`/api/tasks`)
- `GET /` - List user tasks
- `POST /` - Create new task
- `GET /{task_id}` - Get specific task
- `PUT /{task_id}` - Update task
- `DELETE /{task_id}` - Delete task

### Pentesting (`/api/pentest`)
- `POST /scan` - Start security scan
- `GET /scan/{scan_id}` - Get scan results
- `GET /scans` - List all scans
- `DELETE /scan/{scan_id}` - Stop scan
- `GET /network/connections` - Network monitoring
- `GET /threat-dashboard` - Threat metrics

### Reports (`/api/reports`)
- `GET /` - List user reports
- `POST /` - Create new report
- `GET /{report_id}` - Get specific report
- `GET /task/{task_id}` - Get task reports
- `DELETE /{report_id}` - Delete report

## Database Schema

### Users Table
- `id` (Primary Key)
- `username` (Unique)
- `password` (bcrypt hashed)
- `email` (Optional, unique)
- `created_at`
- `is_active`

### Tasks Table
- `id` (Primary Key)
- `user_id` (Foreign Key)
- `name`
- `description`
- `scope`
- `status`
- `created_at`
- `updated_at`

### Agent Logs Table
- `id` (Primary Key)
- `task_id` (Foreign Key)
- `level`
- `message`
- `log_metadata` (JSON)
- `created_at`

### Reports Table
- `id` (Primary Key)
- `task_id` (Foreign Key)
- `user_id` (Foreign Key)
- `title`
- `content`
- `findings` (JSON)
- `severity`
- `created_at`

## Security Features

### JWT Authentication
- 30-minute token expiration
- HS256 algorithm
- Secure secret key configuration
- Bearer token format

### Password Security
- bcrypt hashing with 12 salt rounds
- Minimum 6-character requirement
- Old password verification for changes

### Database Security
- Async PostgreSQL with prepared statements
- Input validation with Pydantic
- SQL injection prevention
- User isolation for data access

## Development Setup

1. **Environment Variables**
   ```bash
   DATABASE_URL=postgresql+asyncpg://user:pass@localhost/drowai
   JWT_SECRET=your-super-secret-key
   ```

2. **Run FastAPI Server**
   ```bash
   python3 run_fastapi.py
   ```

3. **Access API Documentation**
   - Swagger UI: http://localhost:8000/docs
   - ReDoc: http://localhost:8000/redoc

## Migration from Express

The FastAPI backend provides identical functionality to the previous Express.js implementation:

### Maintained Features
- User authentication and session management
- Task CRUD operations
- Pentesting scan simulation
- Report generation
- Real-time threat dashboard
- Network connection monitoring

### Improvements
- Type safety with Pydantic models
- Automatic API documentation
- Async/await for better performance
- Built-in request validation
- Modern Python ecosystem integration

## Testing

### Manual Testing
```bash
# Test authentication
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser","password":"testpass123"}'

# Test protected endpoint
curl -X GET http://localhost:8000/api/auth/me \
  -H "Authorization: Bearer <your-jwt-token>"
```

### Health Check
```bash
curl http://localhost:8000/api/health
```

## Frontend Integration

The React frontend continues to work with the new FastAPI backend:
- Same API endpoints and response formats
- JWT token authentication
- Real-time WebSocket connections
- File upload capabilities

## Production Considerations

1. **Environment Configuration**
   - Use strong JWT secrets
   - Configure CORS properly
   - Enable HTTPS/TLS
   - Set up database connection pooling

2. **Performance**
   - Enable async database operations
   - Use connection pooling
   - Implement caching where appropriate
   - Monitor response times

3. **Security**
   - Regular security audits
   - Token blacklisting for logout
   - Rate limiting implementation
   - Input sanitization

## Terminal Shell (PTY)

- The Container Shell uses a persistent Docker exec PTY for fast, reliable interactive sessions.
- WebSocket entrypoint (frontend default): `/ws?type=terminal&taskId=<id>`
  - Auth: JWT via `sec-websocket-protocol: Bearer.<token>` handled by global `/ws` in `backend/main.py`.
  - Handler: `backend/services/terminal/ws_handler.handle_terminal_ws` via `handle_terminal_websocket`.
- Deprecated alias endpoint: `/api/docker/ws/terminal/{task_id}`
  - Status: deprecated (Phase 6 defer path due confirmed external consumers in Phase 0 audit).
  - Canonical endpoint: `/ws?type=terminal&taskId=<id>`
  - Server emits deprecation telemetry for alias usage and keeps behavior for backward compatibility.

## Docker Logs WebSocket

- WebSocket entrypoint (canonical): `/ws?type=docker&taskId=<id>`
- Deprecated alias endpoint: `/api/docker/ws/logs/{task_id}`
  - Status: deprecated.
  - Canonical endpoint: `/ws?type=docker&taskId=<id>`
  - Server maintains backward compatibility for existing clients while returning deprecation headers and recording alias-usage telemetry.

## Task Metrics WebSocket

- WebSocket entrypoint (canonical): `/ws?type=metrics&taskId=<id>`
- Deprecated alias endpoint: `/api/tasks/ws/tasks/{task_id}/metrics`
  - Status: deprecated (Phase 6 defer path due to confirmed external consumers).
  - Canonical endpoint: `/ws?type=metrics&taskId=<id>`
  - Server maintains backward compatibility for existing clients while returning deprecation headers and recording alias-usage telemetry.

### Message Contract
- Client → Server (text JSON):
  - `{ "type": "ping" }`
  - `{ "type": "create_session" }`
  - `{ "type": "input", "data": "..." }` (raw keystrokes)
  - `{ "type": "resize", "cols": 120, "rows": 30 }`
- Server → Client:
  - `{ "type": "pong" }`
  - `{ "type": "session_created", "session_id": "...", "session": { ... } }`
  - Binary frames containing raw PTY output bytes
  - `{ "type": "error", "message": "..." }`

### Requirements
- Docker SDK “SDK mode” must be available for PTY (`unified_docker_service.start_persistent_pty`).
- Target container for a task must be running or paused.

### Quick Validation
1. Create or select a running task; open Container Shell in the UI.
2. Expect `connection_established` then `session_created` and an interactive shell prompt.
3. Type commands; observe near-instant echo/output.
4. Resize terminal or window; run `stty size` to verify updated rows/cols.
5. Close/reopen the panel; session recreates automatically on reconnect.
