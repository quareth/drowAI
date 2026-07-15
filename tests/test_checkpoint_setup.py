import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

async def test_setup():
    # Load .env file from project root
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ Loaded .env from {env_path}")
    else:
        print(f"⚠️  No .env file found at {env_path}")
    
    # Get the connection string
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("❌ DATABASE_URL not set!")
        return
    
    # Convert SQLAlchemy format to asyncpg format
    db_url = db_url.replace("+psycopg2", "")
    print(f"Database URL: {db_url[:50]}...")
    
    # Create checkpointer
    try:
        async with AsyncPostgresSaver.from_conn_string(db_url) as checkpointer:
            print("✅ Connected to database")
            
            # Run setup
            try:
                print("Running setup()...")
                await checkpointer.setup()
                print("✅ Setup completed successfully!")
                
                # Try to verify tables exist in the same connection
                print("\n🔍 Verifying tables were created...")
                
                # Access the internal connection pool
                conn = checkpointer.conn
                print(f"Connection type: {type(conn)}")
                
                # Try to query the tables
                async with conn.cursor() as cur:
                    await cur.execute("""
                        SELECT tablename 
                        FROM pg_tables 
                        WHERE tablename LIKE 'checkpoint%'
                        ORDER BY tablename
                    """)
                    tables = await cur.fetchall()
                    
                    if tables:
                        print(f"✅ Found {len(tables)} checkpoint table(s):")
                        for table in tables:
                            # Handle dict row
                            table_name = table['tablename'] if isinstance(table, dict) else table[0]
                            print(f"   - {table_name}")
                    else:
                        print("❌ No checkpoint tables found!")
                        
                    # Check current database
                    await cur.execute("SELECT current_database()")
                    db = await cur.fetchone()
                    db_name = db['current_database'] if isinstance(db, dict) else db[0]
                    print(f"\n📊 Current database: {db_name}")
                    
                    # Check if tables exist but in different schema
                    await cur.execute("""
                        SELECT schemaname, tablename 
                        FROM pg_tables 
                        WHERE tablename LIKE 'checkpoint%'
                        ORDER BY schemaname, tablename
                    """)
                    all_tables = await cur.fetchall()
                    if all_tables:
                        print(f"✅ Tables found in schemas:")
                        for row in all_tables:
                            if isinstance(row, dict):
                                print(f"   - {row['schemaname']}.{row['tablename']}")
                            else:
                                print(f"   - {row[0]}.{row[1]}")
                
                print("\nNow check in psql:")
                print("  \\dt checkpoints*")
                
            except Exception as e:
                print(f"❌ Setup/verification failed: {e}")
                import traceback
                traceback.print_exc()
    except Exception as conn_error:
        print(f"❌ Connection failed: {conn_error}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_setup())

