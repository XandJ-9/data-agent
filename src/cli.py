"""Management CLI: init-db, create-user, worker and serve. No public registration."""
import argparse
import getpass
import logging
import sys
from dataclasses import dataclass

import uvicorn

from api import create_app
from errors import ServiceError
from services.accounts import create_user, set_admin
from settings import Settings
from storage import Store
from storage.schema import SCHEMA_VERSION
from worker import Worker

logger = logging.getLogger(__name__)


@dataclass
class Command:
    settings: Settings
    store: Store

    def init_db(self) -> None:
        self.store.init_db()
        logger.info('Database schema initialized (version %s)', SCHEMA_VERSION)

    def create_user(self, username: str, *, admin: bool = False) -> None:
        password = getpass.getpass('Password: ')
        confirm = getpass.getpass('Confirm password: ')
        if password != confirm:
            raise ServiceError(400, 'Passwords do not match')
        user = create_user(self.store, username, password, admin=admin)
        role = 'admin' if admin else 'user'
        logger.info('Created %s %s (%s)', role, user['username'], user['id'])
        print(f"Created user {user['username']} ({user['id']}){' (admin)' if admin else ''}")


def main() -> int:
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    parser = argparse.ArgumentParser(description='Data Agent service')
    parser.add_argument('command', choices=['init-db', 'create-user', 'set-admin', 'worker', 'serve'])
    parser.add_argument('username', nargs='?')
    parser.add_argument('--admin', action='store_true', help='grant the new user admin rights')
    args = parser.parse_args()
    store = Store(settings.resolved_database_url)
    try:
        if args.command == 'init-db':
            Command(settings, store).init_db()
        elif args.command == 'create-user':
            if not args.username:
                parser.error('create-user requires a username')
            store.check_schema()
            Command(settings, store).create_user(args.username, admin=args.admin)
        elif args.command == 'set-admin':
            if not args.username:
                parser.error('set-admin requires a username')
            store.check_schema()
            result = set_admin(store, args.username)
            print(f"{result['username']} admin={result['is_admin']}")
        elif args.command == 'worker':
            Worker(settings).run_forever()
        else:
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    except KeyboardInterrupt:
        return 130
    except (ServiceError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    finally:
        store.dispose()
    return 0


if __name__ == '__main__':
    sys.exit(main())
