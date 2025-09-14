import logging
import sys
import os
from pathlib import Path
from typing import Optional
from datetime import datetime


class Logger:
    """Centralized logging configuration for the application."""
    
    _loggers = {}
    _initialized = False
    
    @classmethod
    def setup_logging(
        cls,
        log_level: str = "INFO",
        log_file: Optional[str] = None,
        log_format: Optional[str] = None
    ) -> None:
        """
        Set up logging configuration for the application.
        
        Args:
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            log_file: Optional log file path
            log_format: Optional custom log format
        """
        if cls._initialized:
            return
            
        # Default log format
        if log_format is None:
            log_format = (
                "%(asctime)s - %(name)s - %(levelname)s - "
                "%(filename)s:%(lineno)d - %(message)s"
            )
        
        # Convert string level to logging constant
        numeric_level = getattr(logging, log_level.upper(), logging.INFO)
        
        # Create logs directory if it doesn't exist
        if log_file:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Configure root logger
        logging.basicConfig(
            level=numeric_level,
            format=log_format,
            handlers=[]
        )
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(numeric_level)
        console_formatter = logging.Formatter(log_format)
        console_handler.setFormatter(console_formatter)
        
        # File handler (if specified)
        handlers = [console_handler]
        if log_file:
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(numeric_level)
            file_formatter = logging.Formatter(log_format)
            file_handler.setFormatter(file_formatter)
            handlers.append(file_handler)
        
        # Add handlers to root logger
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        for handler in handlers:
            root_logger.addHandler(handler)
        
        cls._initialized = True
        
        # Log initialization
        logger = cls.get_logger(__name__)
        logger.info(f"Logging initialized with level: {log_level}")
        if log_file:
            logger.info(f"Log file: {log_file}")
    
    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """
        Get a logger instance for the given name.
        
        Args:
            name: Logger name (usually __name__)
            
        Returns:
            logging.Logger: Configured logger instance
        """
        if not cls._initialized:
            cls.setup_logging()
        
        if name not in cls._loggers:
            logger = logging.getLogger(name)
            cls._loggers[name] = logger
        
        return cls._loggers[name]
    
    @classmethod
    def log_function_call(cls, logger: logging.Logger, func_name: str, **kwargs):
        """
        Log function call with parameters.
        
        Args:
            logger: Logger instance
            func_name: Function name
            **kwargs: Function parameters (excluding sensitive data)
        """
        # Filter out sensitive parameters
        safe_kwargs = {k: v for k, v in kwargs.items() 
                      if not any(sensitive in k.lower() 
                               for sensitive in ['key', 'token', 'secret', 'password'])}
        
        logger.debug(f"Calling {func_name} with params: {safe_kwargs}")
    
    @classmethod
    def log_performance(cls, logger: logging.Logger, operation: str, duration: float):
        """
        Log performance metrics.
        
        Args:
            logger: Logger instance
            operation: Operation name
            duration: Duration in seconds
        """
        logger.info(f"Performance - {operation}: {duration:.2f}s")
    
    @classmethod
    def log_error_with_context(cls, logger: logging.Logger, error: Exception, context: str = ""):
        """
        Log error with additional context.
        
        Args:
            logger: Logger instance
            error: Exception instance
            context: Additional context information
        """
        error_msg = f"Error in {context}: {type(error).__name__}: {str(error)}"
        logger.error(error_msg, exc_info=True)


def get_logger(name: str) -> logging.Logger:
    """
    Convenience function to get a logger instance.
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        logging.Logger: Configured logger instance
    """
    return Logger.get_logger(name)
