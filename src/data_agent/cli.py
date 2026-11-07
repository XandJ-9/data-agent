"""Management CLI: init-db, create-user, worker and serve. No public registration."""
import argparse
import getpass
import sys
from dataclasses import dataclass

import uvicorn

from data_agent.api import create_app
from data_agent.auth import create_user
from data_agent.db import ServiceError, Store
from data_agent.settings import Settings
from data_agent.worker import Worker


@dataclass
class Command:
    settings: Settings
    store: Store

    def init_db(self) -> None:
        self.store.init_db()
        print(f'Initialized schema at {self.store.path}')

    def create_user(self, username: str) -> None:
        password = getpass.getpass('Password: ')
        confirm = getpass.getpass('Confirm password: ')
        if password != confirm:
            raise ServiceError(400, 'Passwords do not match')
        user = create_user(self.store, username, password)
        print(f"Created user {user['username']} ({user['id']})")


def main() -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(description='Data Agent service')
    parser.add_argument('command', choices=['init-db', 'create-user', 'worker', 'serve'])
    parser.add_argument('username', nargs='?')
    args = parser.parse_args()
    store = Store(settings.database_path)
    try:
        if args.command == 'init-db':
            Command(settings, store).init_db()
        elif args.command == 'create-user':
            if not args.username:
                parser.error('create-user requires a username')
            store.check_schema()
            Command(settings, store).create_user(args.username)
        elif args.command == 'worker':
            Worker(settings).run_forever()
        else:
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    except KeyboardInterrupt:
        return 130
    except (ServiceError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
