from __future__ import annotations

import enum
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def enum_type(enum_cls: type[enum.Enum]) -> Enum:
    return Enum(
        enum_cls,
        values_callable=lambda members: [member.value for member in members],
        native_enum=False,
        validate_strings=True,
        create_constraint=True,
        name=f"{enum_cls.__name__.lower()}",
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=func.now(),
    )


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    AUTHOR = "author"


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    LOCKED = "locked"


class BookStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChapterNodeType(str, enum.Enum):
    VOLUME = "volume"
    CHAPTER = "chapter"
    SCENE = "scene"
    NOTE = "note"


class ChapterStatus(str, enum.Enum):
    DRAFT = "draft"
    REVIEW = "review"
    FINAL = "final"
    ARCHIVED = "archived"


class SnapshotKind(str, enum.Enum):
    BEFORE_AI_EDIT = "before_ai_edit"
    MANUAL_SAVE = "manual_save"
    IMPORT = "import"
    ROLLBACK = "rollback"


class AIScope(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    BOOK = "book"


class AIModule(str, enum.Enum):
    CO_WRITING = "co_writing"
    OUTLINE_EXPANSION = "outline_expansion"
    SUMMARY = "summary"
    SETTING_EXTRACTION = "setting_extraction"
    CHARACTER_EXTRACTION = "character_extraction"
    RELATION_EXTRACTION = "relation_extraction"
    REASONER = "reasoner"
    ASSISTANT = "assistant"


class WorldExtractionJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WorldExtractionSource(str, enum.Enum):
    INTERNAL_BOOK = "internal_book"
    IMPORTED_DOCUMENT = "imported_document"


class WorldConflictStrategy(str, enum.Enum):
    MERGE = "merge"
    KEEP_EXISTING = "keep_existing"
    PREFER_IMPORTED = "prefer_imported"
    MANUAL_REVIEW = "manual_review"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_role_status", "role", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128))
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        enum_type(UserRole),
        nullable=False,
        default=UserRole.AUTHOR,
        server_default=text("'author'"),
    )
    status: Mapped[UserStatus] = mapped_column(
        enum_type(UserStatus),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=text("'active'"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
    )
    last_login_at: Mapped[Optional[Any]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    created_by: Mapped[Optional["User"]] = relationship(
        "User",
        remote_side=lambda: [User.id],
        back_populates="created_users",
        foreign_keys=lambda: [User.created_by_id],
    )
    created_users: Mapped[list["User"]] = relationship(
        "User",
        back_populates="created_by",
        foreign_keys=lambda: [User.created_by_id],
    )
    books: Mapped[list["Book"]] = relationship(
        "Book",
        back_populates="owner",
        cascade="all, delete-orphan",
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(
        "Snapshot",
        back_populates="created_by",
    )
    ai_configs: Mapped[list["AIConfig"]] = relationship(
        "AIConfig",
        back_populates="user",
    )
    world_extraction_jobs: Mapped[list["WorldExtractionJob"]] = relationship(
        "WorldExtractionJob",
        back_populates="created_by",
    )


class Book(TimestampMixin, Base):
    __tablename__ = "books"
    __table_args__ = (
        UniqueConstraint("owner_id", "slug", name="uq_books_owner_slug"),
        Index("ix_books_owner_status", "owner_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text)
    genre: Mapped[Optional[str]] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="zh-CN",
        server_default=text("'zh-CN'"),
    )
    tags: Mapped[Optional[list[str]]] = mapped_column(JSON)
    global_style_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    long_term_summary: Mapped[Optional[str]] = mapped_column(Text)
    world_bible: Mapped[Optional[str]] = mapped_column(Text)
    outline: Mapped[Optional[str]] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    chapter_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    status: Mapped[BookStatus] = mapped_column(
        enum_type(BookStatus),
        nullable=False,
        default=BookStatus.DRAFT,
        server_default=text("'draft'"),
    )
    extra_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    owner: Mapped["User"] = relationship("User", back_populates="books")
    chapters: Mapped[list["Chapter"]] = relationship(
        "Chapter",
        back_populates="book",
        cascade="all, delete-orphan",
        order_by=lambda: (Chapter.sort_order.asc(), Chapter.id.asc()),
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(
        "Snapshot",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    characters: Mapped[list["Character"]] = relationship(
        "Character",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    relations: Mapped[list["Relation"]] = relationship(
        "Relation",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    relation_events: Mapped[list["RelationEvent"]] = relationship(
        "RelationEvent",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    factions: Mapped[list["Faction"]] = relationship(
        "Faction",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    faction_memberships: Mapped[list["FactionMembership"]] = relationship(
        "FactionMembership",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    ai_configs: Mapped[list["AIConfig"]] = relationship(
        "AIConfig",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    author_golden_corpus: Mapped[Optional["AuthorGoldenCorpus"]] = relationship(
        "AuthorGoldenCorpus",
        back_populates="book",
        cascade="all, delete-orphan",
        uselist=False,
    )
    semantic_knowledge_entries: Mapped[list["SemanticKnowledgeBase"]] = relationship(
        "SemanticKnowledgeBase",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    world_extraction_jobs: Mapped[list["WorldExtractionJob"]] = relationship(
        "WorldExtractionJob",
        back_populates="book",
        cascade="all, delete-orphan",
    )


class Chapter(TimestampMixin, Base):
    __tablename__ = "chapters"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
        CheckConstraint("depth >= 0", name="depth_non_negative"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_chapters_book_parent_sort", "book_id", "parent_id", "sort_order"),
        Index("ix_chapters_book_tree_path", "book_id", "tree_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    node_type: Mapped[ChapterNodeType] = mapped_column(
        enum_type(ChapterNodeType),
        nullable=False,
        default=ChapterNodeType.CHAPTER,
        server_default=text("'chapter'"),
    )
    status: Mapped[ChapterStatus] = mapped_column(
        enum_type(ChapterStatus),
        nullable=False,
        default=ChapterStatus.DRAFT,
        server_default=text("'draft'"),
    )
    sequence_number: Mapped[Optional[int]] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    tree_path: Mapped[Optional[str]] = mapped_column(String(512))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    outline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context_summary: Mapped[Optional[str]] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    extra_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    book: Mapped["Book"] = relationship("Book", back_populates="chapters")
    parent: Mapped[Optional["Chapter"]] = relationship(
        "Chapter",
        remote_side=lambda: [Chapter.id],
        back_populates="children",
        foreign_keys=lambda: [Chapter.parent_id],
    )
    children: Mapped[list["Chapter"]] = relationship(
        "Chapter",
        back_populates="parent",
        cascade="all, delete-orphan",
        foreign_keys=lambda: [Chapter.parent_id],
        order_by=lambda: (Chapter.sort_order.asc(), Chapter.id.asc()),
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(
        "Snapshot",
        back_populates="chapter",
        cascade="all, delete-orphan",
    )
    episodic_memory: Mapped[Optional["ChapterEpisodicMemory"]] = relationship(
        "ChapterEpisodicMemory",
        back_populates="chapter",
        cascade="all, delete-orphan",
        uselist=False,
    )
    first_appearance_characters: Mapped[list["Character"]] = relationship(
        "Character",
        back_populates="first_appearance_chapter",
        foreign_keys=lambda: [Character.first_appearance_chapter_id],
    )
    last_appearance_characters: Mapped[list["Character"]] = relationship(
        "Character",
        back_populates="last_appearance_chapter",
        foreign_keys=lambda: [Character.last_appearance_chapter_id],
    )


class AIConfig(TimestampMixin, Base):
    __tablename__ = "ai_configs"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        CheckConstraint("timeout_seconds > 0", name="timeout_positive"),
        Index("ix_ai_configs_scope_module_enabled", "scope", "module", "is_enabled"),
        Index("ix_ai_configs_user_module", "user_id", "module"),
        Index("ix_ai_configs_book_module", "book_id", "module"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    scope: Mapped[AIScope] = mapped_column(
        enum_type(AIScope),
        nullable=False,
        default=AIScope.SYSTEM,
        server_default=text("'system'"),
    )
    module: Mapped[AIModule] = mapped_column(enum_type(AIModule), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    book_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("books.id", ondelete="SET NULL"),
        index=True,
    )
    provider_name: Mapped[Optional[str]] = mapped_column(String(64))
    api_format: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="openai_v1",
        server_default=text("'openai_v1'"),
    )
    base_url: Mapped[Optional[str]] = mapped_column(String(512))
    base_url_env_var: Mapped[Optional[str]] = mapped_column(String(128))
    api_key: Mapped[Optional[str]] = mapped_column(Text)
    api_key_env_var: Mapped[Optional[str]] = mapped_column(String(128))
    model_name: Mapped[Optional[str]] = mapped_column(String(128))
    model_name_env_var: Mapped[Optional[str]] = mapped_column(String(128))
    reasoning_effort: Mapped[Optional[str]] = mapped_column(String(32))
    temperature: Mapped[Optional[float]] = mapped_column(Float)
    top_p: Mapped[Optional[float]] = mapped_column(Float)
    max_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=120,
        server_default=text("120"),
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        server_default=text("100"),
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    system_prompt_template: Mapped[Optional[str]] = mapped_column(Text)
    extra_headers: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    extra_body: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    user: Mapped[Optional["User"]] = relationship("User", back_populates="ai_configs")
    book: Mapped[Optional["Book"]] = relationship("Book", back_populates="ai_configs")
    snapshots: Mapped[list["Snapshot"]] = relationship(
        "Snapshot",
        back_populates="ai_config",
    )


class Snapshot(TimestampMixin, Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        CheckConstraint("chapter_version >= 1", name="chapter_version_positive"),
        Index("ix_snapshots_book_created", "book_id", "created_at"),
        Index("ix_snapshots_chapter_created", "chapter_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chapter_id: Mapped[int] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    ai_config_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ai_configs.id", ondelete="SET NULL"),
        index=True,
    )
    kind: Mapped[SnapshotKind] = mapped_column(
        enum_type(SnapshotKind),
        nullable=False,
        default=SnapshotKind.BEFORE_AI_EDIT,
        server_default=text("'before_ai_edit'"),
    )
    label: Mapped[Optional[str]] = mapped_column(String(128))
    chapter_title: Mapped[str] = mapped_column(String(255), nullable=False)
    chapter_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    outline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[Optional[str]] = mapped_column(Text)
    source_model_name: Mapped[Optional[str]] = mapped_column(String(128))
    prompt_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    diff_summary: Mapped[Optional[str]] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    character_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    book: Mapped["Book"] = relationship("Book", back_populates="snapshots")
    chapter: Mapped["Chapter"] = relationship("Chapter", back_populates="snapshots")
    created_by: Mapped[Optional["User"]] = relationship("User", back_populates="snapshots")
    ai_config: Mapped[Optional["AIConfig"]] = relationship(
        "AIConfig",
        back_populates="snapshots",
    )


class ChapterEpisodicMemory(TimestampMixin, Base):
    __tablename__ = "chapter_episodic_memories"
    __table_args__ = (
        UniqueConstraint("chapter_id", name="uq_chapter_episodic_memories_chapter_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chapter_id: Mapped[int] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    summary: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    involved_characters: Mapped[Optional[str]] = mapped_column(String(512))

    chapter: Mapped["Chapter"] = relationship("Chapter", back_populates="episodic_memory")


class AuthorGoldenCorpus(TimestampMixin, Base):
    __tablename__ = "author_golden_corpora"
    __table_args__ = (
        UniqueConstraint("book_id", name="uq_author_golden_corpora_book_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    book: Mapped["Book"] = relationship("Book", back_populates="author_golden_corpus")


class SemanticKnowledgeBase(TimestampMixin, Base):
    __tablename__ = "semantic_knowledge_base"
    __table_args__ = (
        UniqueConstraint("book_id", "entity_name", name="uq_semantic_knowledge_base_book_entity"),
        Index("ix_semantic_knowledge_base_book_entity", "book_id", "entity_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_name: Mapped[str] = mapped_column(String(255), nullable=False)
    core_fact: Mapped[str] = mapped_column(Text, nullable=False, default="")

    book: Mapped["Book"] = relationship("Book", back_populates="semantic_knowledge_entries")


class Character(TimestampMixin, Base):
    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint("book_id", "name", name="uq_characters_book_name"),
        Index("ix_characters_book_name", "book_id", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[Optional[list[str]]] = mapped_column(JSON)
    role_label: Mapped[Optional[str]] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text)
    traits: Mapped[Optional[list[str]]] = mapped_column(JSON)
    background: Mapped[Optional[str]] = mapped_column(Text)
    goals: Mapped[Optional[str]] = mapped_column(Text)
    secrets: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    first_appearance_chapter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"),
        index=True,
    )
    last_appearance_chapter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"),
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
    )
    card_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    book: Mapped["Book"] = relationship("Book", back_populates="characters")
    first_appearance_chapter: Mapped[Optional["Chapter"]] = relationship(
        "Chapter",
        back_populates="first_appearance_characters",
        foreign_keys=[first_appearance_chapter_id],
    )
    last_appearance_chapter: Mapped[Optional["Chapter"]] = relationship(
        "Chapter",
        back_populates="last_appearance_characters",
        foreign_keys=[last_appearance_chapter_id],
    )
    outgoing_relations: Mapped[list["Relation"]] = relationship(
        "Relation",
        back_populates="source_character",
        foreign_keys=lambda: [Relation.source_character_id],
    )
    incoming_relations: Mapped[list["Relation"]] = relationship(
        "Relation",
        back_populates="target_character",
        foreign_keys=lambda: [Relation.target_character_id],
    )
    source_relation_events: Mapped[list["RelationEvent"]] = relationship(
        "RelationEvent",
        back_populates="source_character",
        foreign_keys=lambda: [RelationEvent.source_character_id],
    )
    target_relation_events: Mapped[list["RelationEvent"]] = relationship(
        "RelationEvent",
        back_populates="target_character",
        foreign_keys=lambda: [RelationEvent.target_character_id],
    )
    faction_memberships: Mapped[list["FactionMembership"]] = relationship(
        "FactionMembership",
        back_populates="character",
        cascade="all, delete-orphan",
    )


class Relation(TimestampMixin, Base):
    __tablename__ = "relations"
    __table_args__ = (
        CheckConstraint(
            "source_character_id <> target_character_id",
            name="source_target_distinct",
        ),
        UniqueConstraint(
            "book_id",
            "source_character_id",
            "target_character_id",
            "relation_type",
            name="uq_relations_book_source_target_type",
        ),
        Index(
            "ix_relations_book_source_target",
            "book_id",
            "source_character_id",
            "target_character_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text)
    strength: Mapped[Optional[float]] = mapped_column(Float)
    importance_level: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="major",
        server_default=text("'major'"),
    )
    is_bidirectional: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    valid_from_chapter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL")
    )
    valid_to_chapter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL")
    )
    extra_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    book: Mapped["Book"] = relationship("Book", back_populates="relations")
    source_character: Mapped["Character"] = relationship(
        "Character",
        back_populates="outgoing_relations",
        foreign_keys=[source_character_id],
    )
    target_character: Mapped["Character"] = relationship(
        "Character",
        back_populates="incoming_relations",
        foreign_keys=[target_character_id],
    )
    valid_from_chapter: Mapped[Optional["Chapter"]] = relationship(
        "Chapter",
        foreign_keys=[valid_from_chapter_id],
    )
    valid_to_chapter: Mapped[Optional["Chapter"]] = relationship(
        "Chapter",
        foreign_keys=[valid_to_chapter_id],
    )
    events: Mapped[list["RelationEvent"]] = relationship(
        "RelationEvent",
        back_populates="relation",
        cascade="all, delete-orphan",
        order_by=lambda: (RelationEvent.chapter_id.asc(), RelationEvent.id.asc()),
    )


class RelationEvent(TimestampMixin, Base):
    __tablename__ = "relation_events"
    __table_args__ = (
        Index("ix_relation_events_relation_chapter", "relation_id", "chapter_id", "id"),
        Index("ix_relation_events_book_source_target", "book_id", "source_character_id", "target_character_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    relation_id: Mapped[int] = mapped_column(
        ForeignKey("relations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chapter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"),
        index=True,
    )
    segment_label: Mapped[Optional[str]] = mapped_column(String(255))
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text)
    strength: Mapped[Optional[float]] = mapped_column(Float)
    importance_level: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="major",
        server_default=text("'major'"),
    )
    is_bidirectional: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    event_summary: Mapped[Optional[str]] = mapped_column(Text)

    relation: Mapped["Relation"] = relationship("Relation", back_populates="events")
    book: Mapped["Book"] = relationship("Book", back_populates="relation_events")
    source_character: Mapped["Character"] = relationship(
        "Character",
        back_populates="source_relation_events",
        foreign_keys=[source_character_id],
    )
    target_character: Mapped["Character"] = relationship(
        "Character",
        back_populates="target_relation_events",
        foreign_keys=[target_character_id],
    )
    chapter: Mapped[Optional["Chapter"]] = relationship(
        "Chapter",
        foreign_keys=[chapter_id],
    )


class Faction(TimestampMixin, Base):
    __tablename__ = "factions"
    __table_args__ = (
        UniqueConstraint("book_id", "name", name="uq_factions_book_name"),
        Index("ix_factions_book_name", "book_id", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    color: Mapped[Optional[str]] = mapped_column(String(32))
    extra_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    book: Mapped["Book"] = relationship("Book", back_populates="factions")
    memberships: Mapped[list["FactionMembership"]] = relationship(
        "FactionMembership",
        back_populates="faction",
        cascade="all, delete-orphan",
    )


class FactionMembership(TimestampMixin, Base):
    __tablename__ = "faction_memberships"
    __table_args__ = (
        Index("ix_faction_memberships_book_character", "book_id", "character_id"),
        Index("ix_faction_memberships_faction_character", "faction_id", "character_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    faction_id: Mapped[int] = mapped_column(
        ForeignKey("factions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_label: Mapped[Optional[str]] = mapped_column(String(128))
    loyalty: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    start_chapter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL")
    )
    end_chapter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL")
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)

    book: Mapped["Book"] = relationship("Book", back_populates="faction_memberships")
    faction: Mapped["Faction"] = relationship("Faction", back_populates="memberships")
    character: Mapped["Character"] = relationship("Character", back_populates="faction_memberships")
    start_chapter: Mapped[Optional["Chapter"]] = relationship(
        "Chapter",
        foreign_keys=[start_chapter_id],
    )
    end_chapter: Mapped[Optional["Chapter"]] = relationship(
        "Chapter",
        foreign_keys=[end_chapter_id],
    )


class WorldExtractionJob(TimestampMixin, Base):
    __tablename__ = "world_extraction_jobs"
    __table_args__ = (
        Index("ix_world_extraction_jobs_book_created", "book_id", "created_at"),
        Index("ix_world_extraction_jobs_book_status", "book_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    source_type: Mapped[WorldExtractionSource] = mapped_column(
        enum_type(WorldExtractionSource),
        nullable=False,
    )
    source_name: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[WorldExtractionJobStatus] = mapped_column(
        enum_type(WorldExtractionJobStatus),
        nullable=False,
        default=WorldExtractionJobStatus.PENDING,
        server_default=text("'pending'"),
    )
    conflict_strategy: Mapped[WorldConflictStrategy] = mapped_column(
        enum_type(WorldConflictStrategy),
        nullable=False,
        default=WorldConflictStrategy.MERGE,
        server_default=text("'merge'"),
    )
    update_world_bible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
    )
    chapter_scope: Mapped[Optional[str]] = mapped_column(String(32))
    segment_unit_limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5000,
        server_default=text("5000"),
    )
    total_units: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    processed_units: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    total_segments: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    processed_segments: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    message: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[Any]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[Any]] = mapped_column(DateTime(timezone=True))
    options_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    result_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    book: Mapped["Book"] = relationship("Book", back_populates="world_extraction_jobs")
    created_by: Mapped[Optional["User"]] = relationship("User", back_populates="world_extraction_jobs")


__all__ = [
    "AIConfig",
    "AIModule",
    "AIScope",
    "AuthorGoldenCorpus",
    "Base",
    "Book",
    "BookStatus",
    "Chapter",
    "ChapterEpisodicMemory",
    "ChapterNodeType",
    "ChapterStatus",
    "Character",
    "Relation",
    "SemanticKnowledgeBase",
    "Snapshot",
    "SnapshotKind",
    "User",
    "UserRole",
    "UserStatus",
    "WorldConflictStrategy",
    "WorldExtractionJob",
    "WorldExtractionJobStatus",
    "WorldExtractionSource",
]
